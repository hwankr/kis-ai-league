"""Explicit remaining-session quote prefix, never a rewritten close contract.

Frozen prior-close signals meet observed quotes once during today's remaining
session. Their conditional fills, cash, costs and new lot ages carry into each
following analogue path. No modelled amount is written to the actual ledger.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from backend.daily_inputs import validate_award_context
from backend.learning_policy import (FEE, TAX, KST, _initial_state, portfolio_metrics,
    validate_policy, signals, canonical_entry_context)
from backend.policy_transition import transition_preview as close_transition_preview
from backend import policy_award_scenarios as daily


VERSION = 'intraday-award-context-v1'
BASIS = 'remaining_intraday_observed_quote_prefix'
PLAN_VERSION = 'intraday-quality-only-common-windows-v1'
_hash = daily._hash


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('invalid_intraday_day')
    return value


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError('invalid_intraday_amount')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('invalid_intraday_amount')
    return result


def _future_days(day, end):
    values, current = [], date.fromisoformat(day)
    while current < date.fromisoformat(end):
        current += timedelta(days=1)
        if current.weekday() < 5:
            values.append(current.isoformat())
    return values


def _validate(value):
    required = {'version', 'basis', 'source_award_context', 'source_award_context_hash',
        'current_book', 'current_book_hash', 'initial_state', 'observed_at', 'activation_day',
        'competition_context', 'session_days', 'broker_calendar_evidence', 'today_order_count',
        'today_fill_count', 'account_blockers', 'latest_frame', 'limits', 'signal_day',
        'current_quotes', 'current_quotes_hash', 'hash'}
    if (not isinstance(value, dict) or set(value) != required or value['version'] != VERSION
            or value['basis'] != BASIS
            or value['hash'] != _hash({key: item for key, item in value.items() if key != 'hash'})):
        raise ValueError('invalid_intraday_context')
    source, current = value['source_award_context'], value['current_book']
    if (not validate_award_context(source) or value['source_award_context_hash'] != source['hash']
            or value['current_book_hash'] != _hash(current) or value['initial_state'] != current
            or any(value[key] != source[key] for key in ('latest_frame', 'limits', 'signal_day'))):
        raise ValueError('intraday_source_changed')
    day, prior = _day(value['activation_day']), _day(source['signal_day'])
    observed = datetime.fromisoformat(value['observed_at'])
    if (observed.utcoffset() is None or observed.astimezone(KST).date().isoformat() != day
            or not time(9, 5) <= observed.astimezone(KST).time() < time(15, 15)
            or datetime.fromisoformat(source['observed_at']) >= observed or not prior < day):
        raise ValueError('invalid_intraday_observation')
    quotes = value['current_quotes']
    if (not isinstance(quotes, dict) or not quotes or value['current_quotes_hash'] != _hash(quotes)):
        raise ValueError('intraday_quotes_unavailable')
    universe = {item['symbol'] for item in source['latest_frame']['rows']}
    for symbol, quote in quotes.items():
        if (symbol not in universe or not isinstance(quote, dict)
                or type(quote.get('eligible')) is not bool or type(quote.get('buy_eligible', True)) is not bool
                or not _decimal(quote.get('price')) or _decimal(quote.get('volume')) < 0):
            raise ValueError('invalid_intraday_quote')
        quote_at = datetime.fromisoformat(quote.get('observed_at', ''))
        if (quote_at.utcoffset() is None or quote_at.astimezone(KST).date().isoformat() != day
                or not 0 <= (observed - quote_at).total_seconds() <= 180):
            raise ValueError('intraday_quote_stale_at_freeze')
        for field in ('limit_price', 'buy_limit_price', 'sell_limit_price'):
            if field in quote and not _decimal(quote[field]):
                raise ValueError('invalid_intraday_quote')
    days, calendar = value['session_days'], value['broker_calendar_evidence']
    if (not isinstance(days, list) or days != sorted(set(days))
            or any(_day(item) > day for item in days)
            or days != source['latest_frame']['session_days'] + [day]
            or not isinstance(calendar, dict) or calendar.get('date') != day
            or calendar.get('is_trading_day') is not True
            or not calendar.get('source') or not calendar.get('observed_at')):
        raise ValueError('intraday_calendar_unverified')
    calendar_at = datetime.fromisoformat(calendar['observed_at'])
    if (calendar_at.utcoffset() is None or calendar_at > observed
            or calendar_at.astimezone(KST).date().isoformat() != day):
        raise ValueError('intraday_calendar_unverified')
    progress = value['competition_context']
    if (not isinstance(progress, dict) or progress.get('contest') != 'kis_ai_2026'
            or progress.get('reliable') is not True or progress.get('period', {}).get('as_of') != day):
        raise ValueError('intraday_contest_progress_unavailable')
    original_progress = source['competition_context']
    if (not isinstance(original_progress, dict) or original_progress.get('reliable') is not True
            or _decimal(progress['pace']['gross_amount']) != _decimal(original_progress['pace']['gross_amount'])
            or progress.get('metrics') != original_progress.get('metrics')
            or any(progress['period'][key] != original_progress['period'][key]
                   for key in ('start_date', 'end_date'))):
        raise ValueError('intraday_contest_ledger_changed')
    future = _future_days(day, _day(progress['period']['end_date']))
    remaining = progress.get('pace', {}).get('remaining_sessions')
    if (type(remaining) is not int or remaining != len(future) or remaining < 3
            or _day(calendar.get('next_session')) != future[0]):
        raise ValueError('intraday_forward_horizon_mismatch')
    if (any(type(value[key]) is not int or value[key] != 0 for key in ('today_order_count', 'today_fill_count'))
            or not isinstance(value['account_blockers'], list)
            or any(not isinstance(item, str) or not item for item in value['account_blockers'])):
        raise ValueError('intraday_ledger_changed')
    original = source['initial_state']
    if current.get('as_of') != day:
        raise ValueError('intraday_book_date_mismatch')
    money = {key: _decimal(source['limits'][key]) for key in ('budget', 'order_cap', 'daily_buy_limit')}
    for book in (original, current):
        _initial_state(book, money['budget'], book['as_of'])
        if (book.get('pending_orders') != 0 or book.get('unresolved_orders') != 0
                or _decimal(book.get('reserved_cash')) != 0 or book.get('reserved_entries') != []
                or book.get('pending_entries', []) != []):
            raise ValueError('intraday_orders_not_settled')
    if any(_decimal(current[key]) != _decimal(original[key]) for key in ('cash', 'budget', 'reserved_cash')):
        raise ValueError('intraday_capital_changed')
    old_positions = {item['symbol']: item for item in original['positions']}
    positions = {item['symbol']: item for item in current['positions']}
    if old_positions.keys() != positions.keys():
        raise ValueError('intraday_positions_changed')
    closes = {item['symbol']: _decimal(item['close']) for item in source['latest_frame']['rows']}
    for symbol, lot in positions.items():
        old = old_positions[symbol]
        keys = {'symbol', 'strategy_id', 'quantity', 'entry_date', 'exit_policy', 'exit_pending'}
        if (any(lot.get(key) != old.get(key) for key in keys)
                or any(_decimal(lot[key]) != _decimal(old[key]) for key in ('cost', 'entry_price'))
                or _day(lot.get('entry_date')) > prior
                or lot['held_sessions'] != sum(lot['entry_date'] < item <= day for item in days)
                or old['held_sessions'] != sum(old['entry_date'] < item <= prior for item in days)
                or lot['held_sessions'] != old['held_sessions'] + 1
                or _decimal(old['last_close']) != closes[symbol]
                or lot.get('exit_fill_remainder', 0) != old.get('exit_fill_remainder', 0)
                or lot.get('exit_delay_remaining') != old.get('exit_delay_remaining')):
            raise ValueError('intraday_lot_changed')
        if symbol not in quotes or _decimal(lot['last_close']) != _decimal(quotes[symbol]['price']):
            raise ValueError('intraday_holding_quote_mismatch')
    expected_equity = _decimal(current['cash']) + sum((_decimal(item['last_close']) * item['quantity']
        * (1 - FEE - TAX) for item in positions.values()), Decimal(0))
    if abs(_decimal(current['equity']) - expected_equity) > Decimal('.000001'):
        raise ValueError('intraday_equity_mismatch')
    old_risk, risk = original['account_protection'], current['account_protection']
    if (set(risk) != {'units', 'peak', 'active', 'limit_pct'} or type(risk['active']) is not bool
            or _decimal(risk['units']) != _decimal(old_risk['units']) or not _decimal(risk['units'])
            or _decimal(risk['peak']) < _decimal(old_risk['peak']) or not _decimal(risk['peak'])
            or _decimal(risk['limit_pct']) != _decimal(old_risk['limit_pct'])
            or _decimal(risk['limit_pct']) != Decimal(15) or old_risk['active'] and not risk['active']):
        raise ValueError('intraday_protection_changed')
    return source, current, future


def validate_intraday_context(context):
    try:
        _validate(context)
        return True
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return False


def freeze_intraday_context(*, source_award_context, current_book, observed_at, activation_day,
                           competition_context, session_days, broker_calendar_evidence,
                           today_order_count, today_fill_count, current_quotes, account_blockers=()):
    value = {'version': VERSION, 'basis': BASIS,
        'source_award_context': deepcopy(source_award_context),
        'source_award_context_hash': source_award_context['hash'],
        'current_book': deepcopy(current_book), 'current_book_hash': _hash(current_book),
        'initial_state': deepcopy(current_book), 'observed_at': observed_at,
        'activation_day': activation_day, 'competition_context': deepcopy(competition_context),
        'session_days': deepcopy(session_days), 'broker_calendar_evidence': deepcopy(broker_calendar_evidence),
        'today_order_count': today_order_count, 'today_fill_count': today_fill_count,
        'current_quotes': deepcopy(current_quotes), 'current_quotes_hash': _hash(current_quotes),
        'account_blockers': list(account_blockers),
        **{key: deepcopy(source_award_context[key]) for key in ('latest_frame', 'limits', 'signal_day')}}
    value['hash'] = _hash(value)
    _validate(value)
    return value


def _projection(context):
    source, current, future = _validate(context)
    book = deepcopy(source['initial_state'])
    book['as_of'] = context['activation_day']
    for item in book['positions']:
        item['held_sessions'] += 1
    book['account_protection'] = deepcopy(current['account_protection'])
    return source, book, future


def _prefix_book(context):
    source, current, _ = _validate(context)
    book = deepcopy(source['initial_state'])
    book['account_protection'] = deepcopy(current['account_protection'])
    return book


def _prefix_inputs(policy, context):
    source, current, _ = _validate(context)
    original = deepcopy(source['latest_frame'])
    book = _prefix_book(context)
    generated = signals(policy, original, entry_context=(canonical_entry_context(book)
        if policy.get('held_before_top_n', False) else None))
    required = {item['symbol'] for item in generated} | {item['symbol'] for item in book['positions']}
    if not required <= context['current_quotes'].keys():
        raise ValueError('intraday_candidate_quote_missing')
    partial = deepcopy(original)
    partial.update(as_of=context['activation_day'],
        observed_at=context['activation_day']+'T16:05:00+09:00',
        session_days=context['session_days'], execution_quotes=deepcopy(context['current_quotes']),
        simulation_partial_session={'observed_close': False, 'source_frame_hash': _hash(original),
            'actual_quote_snapshot_at': context['observed_at'], 'quote_hash': context['current_quotes_hash'],
            'assumption': 'observed_quotes_remain_unchanged_until_model_close'})
    for row in partial['rows']:
        quote = context['current_quotes'].get(row['symbol'])
        if quote is None:
            continue
        price = float(_decimal(quote['price']))
        # Synthetic range only bounds the observed quote, not an observed OHLC bar.
        row.update(open=price, close=price, high=max(float(row['high']), price),
                   low=min(float(row['low']), price), volume=float(_decimal(quote['volume'])))
        if _decimal(quote['volume']) == 0:
            partial['execution_quotes'][row['symbol']]['eligible'] = False
    return original, partial, book


def _prefix(policy, limits, context, *, partial_fills):
    original, partial, book = _prefix_inputs(policy, context)
    model = {'mode': 'observed_quotes', 'fee_rate': FEE, 'tax_rate': TAX,
        'slippage_bps': 0, 'stress_slippage_bps': 0, 'stress_fee_rate': FEE + Decimal('.001'),
        'daily_buy_caps': {context['activation_day']: limits['daily_buy_limit']}}
    if partial_fills:
        model.update(fill_ratio=.5, sell_delay_sessions=1)
    metric = portfolio_metrics(policy, [original, partial], limits,
        initial_state=book, execution_model=model)
    if not metric['valid']:
        raise ValueError(metric['error'])
    return metric, partial, book


def _provenance(context, book):
    return {'version': VERSION, 'basis': BASIS, 'context_hash': context['hash'],
        'source_award_context_hash': context['source_award_context_hash'],
        'source_book_hash': _hash(context['source_award_context']['initial_state']),
        'current_book_hash': context['current_book_hash'], 'simulation_book_hash': _hash(book),
        'source_frame_hash': _hash(context['latest_frame']), 'activation_day': context['activation_day'],
        'observed_at': context['observed_at'], 'current_quotes_hash': context['current_quotes_hash'],
        'observed_close_on_activation_day': False, 'intraday_fill_effects_included': True,
        'actual_ledger_gross_added': 0,
        'assumptions': ['Frozen prior-close signals meet frozen observed quotes once in the remaining session.',
            'Quote prices remain unchanged until the model close; unquoted symbols retain prior closes.',
            'Today has full or 50 percent conditional fills; costs, cash and new lot age zero carry forward.',
            'The original daily cap is unused because this contract requires zero actual orders and fills today.',
            'Existing exit rules and current protection remain; future analogue prices are not forecasts.',
            'Normal quotes use actual commission; today cost stress adds 10bp commission without worse-than-limit fills.']}


def transition_preview(policy, frame, limits, context):
    try:
        source, current, _ = _validate(context)
        if frame != source['latest_frame'] or limits != source['limits']:
            raise ValueError('intraday_source_changed')
        close = deepcopy(source)
        close['initial_state']['account_protection'] = deepcopy(current['account_protection'])
        close['account_blockers'] = deepcopy(context['account_blockers'])
        result = close_transition_preview(policy, frame, limits, close)
        result.update(basis='reference_close_conditional_not_livequote',
            intended_session=context['activation_day'], observed_at=context['observed_at'],
            source_award_context_hash=source['hash'], current_book_hash=context['current_book_hash'],
            scenario_fill_evidence=False)
        result['assumptions'].append('Original close selection with unchanged economics; current live quote checks remain mandatory.')
        if result['status'] == 'ready':
            result['reference_close_capacity'] = {key: deepcopy(result.get(key)) for key in
                ('entry_count', 'gross_buy_amount', 'entry_cost', 'entries', 'zero_entry_reason', 'exits',
                 'cash_after_entries', 'protection')}
            metric, _, _ = _prefix(validate_policy(deepcopy(policy)), limits, context, partial_fills=False)
            previous = {item['symbol'] for item in current['positions']}
            entries = [{'symbol': item['symbol'], 'strategy_id': item['strategy_id'],
                'quantity': item['quantity'], 'price': item['entry_price'],
                'estimated_cost': item['cost'], 'exit_policy': deepcopy(item['exit_policy'])}
                for item in metric['final_state']['positions'] if item['symbol'] not in previous]
            activity = metric['trading_activity']
            result.update(basis='observed_quote_conditional_remaining_session_capacity',
                entry_count=len(entries), entries=entries, gross_buy_amount=str(activity['gross_buy_amount']),
                gross_sell_amount=str(activity['gross_sell_amount']),
                entry_cost=str(sum((Decimal(item['estimated_cost']) for item in entries), Decimal(0))),
                cash_after_entries=metric['final_state']['cash'],
                zero_entry_reason=None if entries else result['zero_entry_reason'] or 'current_quote_or_execution_gate',
                quote_hash=context['current_quotes_hash'], quote_snapshot_at=context['observed_at'],
                execution_diagnostics=deepcopy(metric['diagnostics']['normal']),
                conditional_only=True, actual_orders_sent=0)
            if metric['final_state'].get('account_protection'):
                result['protection'].update(metric['final_state']['account_protection'])
                final = metric['final_state']
                marked = Decimal(final['cash']) + sum((Decimal(item['last_close']) * item['quantity']
                    * (1-FEE-TAX) for item in final['positions']), Decimal(0))
                result['protection']['drawdown_pct'] = float((1-marked
                    / Decimal(result['protection']['units']) / Decimal(result['protection']['peak']))*100)
                if result['protection']['active']:
                    result['zero_entry_reason'] = 'account_protection_active'
        return result
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as error:
        return {'status': 'unavailable', 'basis': 'reference_close_conditional_not_livequote',
                'reasons': [daily._reason(error, 'invalid_intraday_context')]}


def scenario_plan(frames, context):
    try:
        source, _, future = _projection(context)
        book = _prefix_book(context)
        if not isinstance(frames, list) or not frames or frames[-1] != source['latest_frame']:
            raise ValueError('intraday_history_source_mismatch')
        daily._history(frames, source['latest_frame'])
        seed = deepcopy(source)
        seed.update(initial_state=book, competition_context=deepcopy(context['competition_context']))
        result = daily.scenario_plan(frames, seed)
        result.update(version=PLAN_VERSION, projection=_provenance(context, book),
            assumed_start=future[0], assumed_end=future[-1],
            calendar_basis='following_weekdays_estimate_with_two_session_shortening')
        result['hash'] = _hash({key: item for key, item in result.items() if key != 'hash'})
        return result
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as error:
        return {'version': PLAN_VERSION, 'status': 'unavailable',
                'reason': daily._reason(error, 'invalid_intraday_context'), 'schedules': []}


def _nav_curve(metric, stress):
    prefix = 'stress_' if stress else ''
    diagnostics = metric['diagnostics']['cost_stress' if stress else 'normal']
    discount = ((1 - Decimal(str(diagnostics['slippage_bps'])) / 10000)
        * (1 - Decimal(str(diagnostics['fee_rate'])) - Decimal(str(diagnostics['tax_rate']))))
    return [Decimal(str(point['cash'])) + Decimal(str(point['position_value'])) / discount
        for point in metric[prefix+'equity_curve']]


def _combined_outcome(prefix_metric, future_metric, *, stress):
    before, after = (daily._outcome(metric, stress=stress) for metric in (prefix_metric, future_metric))
    result = deepcopy(after)
    for key in ('gross_amount', 'gross_buy_amount', 'gross_sell_amount', 'trading_days',
                'buy_fill_events', 'sell_fill_events', 'fees_paid', 'taxes_paid'):
        result[key] += before[key]
    # Counts alone cannot identify the union of names traded in both legs.
    result['symbols'] = max(before['symbols'], after['symbols'])
    result['symbols_basis'] = 'conservative_union_lower_bound_across_prefix_and_future'
    result['first_buy_session'] = 0 if before['gross_buy_amount'] else after['first_buy_session']
    curve = _nav_curve(prefix_metric, stress) + _nav_curve(future_metric, stress)[1:]
    peak, drawdown = curve[0], Decimal(0)
    for equity in curve:
        peak = max(peak, equity)
        drawdown = max(drawdown, (1-equity/peak)*100)
    result['max_drawdown_pct'] = float(drawdown)
    for key in ('buy_attempts', 'sell_attempts', 'partial_orders', 'unfilled_orders',
                'limit_blocked', 'untradeable_bars', 'ineligible_quotes', 'fees_paid', 'taxes_paid'):
        result['execution_diagnostics'][key] += before['execution_diagnostics'][key]
    result['execution_diagnostics']['slippage_basis'] = 'following_analogue_leg_only'
    result['intraday_prefix'] = before
    result['post_prefix_book_hash'] = _hash(prefix_metric[('stress_' if stress else '')+'final_state'])
    result['intraday_modelled_fills_are_actual'] = False
    return result


def award_scenarios(policy, frames, limits, context, *, plan=None):
    result = daily._base()
    result.update(version=VERSION, basis=BASIS)
    try:
        source, _, future = _projection(context)
        book = _prefix_book(context)
        policy = validate_policy(deepcopy(policy))
        preview = transition_preview(policy, context['latest_frame'], limits, context)
        if preview['status'] != 'ready':
            raise ValueError(preview['reasons'][0])
        expected = scenario_plan(frames, context)
        if plan is not None and plan != expected:
            raise ValueError('intraday_scenario_plan_mismatch')
        plan = expected
        if not plan.get('schedules'):
            raise ValueError(plan['reason'])
        costs = daily._costs(None)
        result.update(scenario_plan=deepcopy(plan), scenario_plan_hash=plan['hash'],
            projection=_provenance(context, book), horizon=len(future),
            calendar_basis='following_weekdays_estimate', start_book_hash=_hash(book),
            current_frame_hash=_hash(source['latest_frame']), history_hash=_hash(frames), cost_profile=costs,
            execution_context_basis='fixed_analogue_assumptions')
        result['assumptions'] = result['projection']['assumptions'] + result['assumptions']
        prefixes = {}
        for partial_fills in (False, True):
            prefixes[partial_fills] = _prefix(policy, limits, context, partial_fills=partial_fills)
        result['intraday_prefixes'] = {('partial_delayed' if partial else 'normal'): {
            'normal': daily._outcome(metric, stress=False),
            'cost_stress': daily._outcome(metric, stress=True),
            'frame_hash': _hash(anchor), 'pre_prefix_book_hash': _hash(initial)}
            for partial, (metric, anchor, initial) in prefixes.items()}
        for schedule in plan['schedules']:
            for record in schedule['selected']:
                window, start, length = record['window'], record['source_start_index'], schedule['horizon']
                variants = [('normal', False, False), ('cost_stress', True, False),
                    ('partial_delayed', False, True), ('partial_delayed_cost_stress', True, True)]
                try:
                    if record['status'] != 'ready':
                        raise ValueError(record['reason'])
                    historical = frames[start:start + length + 1]
                    metrics = {}
                    for environment, stress, partial in variants:
                        identity = f"{window}_{schedule['schedule']}_{environment}"
                        prefix_metric, anchor, initial = prefixes[partial]
                        states = {'normal': deepcopy(prefix_metric['final_state']),
                                  'stress': deepcopy(prefix_metric['stress_final_state'])}
                        # Include every post-prefix holding in source coverage.
                        path_book = {'positions': states['normal']['positions'] + [item for item in states['stress']['positions']
                            if item['symbol'] not in {lot['symbol'] for lot in states['normal']['positions']} ]}
                        path = daily._path(policy, historical, anchor, path_book)
                        if partial not in metrics:
                            model = {'mode': 'daily_open', **costs}
                            if partial:
                                model.update(fill_ratio=.5, sell_delay_sessions=1)
                            metrics[partial] = portfolio_metrics(policy, path, limits,
                                initial_state=states, execution_model=model)
                        metric = metrics[partial]
                        item = daily._unavailable(identity, window, length, environment, metric.get('error'))
                        item.update(schedule=schedule['schedule'], source_start=historical[0]['as_of'],
                            source_end=historical[-1]['as_of'], assumed_start=path[1]['as_of'],
                            assumed_end=path[-1]['as_of'],
                            intraday_assumed_start=context['activation_day'],
                            path_hash=_hash({'prefix_source_frame': source['latest_frame'],
                                'prefix_frame': anchor, 'future_path': path}), source_start_index=start,
                            source_intervals=length, start_book_hash=result['start_book_hash'],
                            scenario_plan_hash=plan['hash'])
                        if metric['valid']:
                            item.update(_combined_outcome(prefix_metric, metric, stress=stress))
                        result['scenarios'].append(item)
                except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as error:
                    for environment, _, _ in variants:
                        item = daily._unavailable(f"{window}_{schedule['schedule']}_{environment}",
                            window, length, environment, daily._reason(error))
                        item.update(schedule=schedule['schedule'], scenario_plan_hash=plan['hash'],
                            source_start_index=start)
                        result['scenarios'].append(item)
        ready = sum(item['status'] == 'ready' for item in result['scenarios'])
        result.update(status='ready' if ready == len(result['scenarios']) else 'unavailable',
            ready_scenarios=ready, scenario_count=len(result['scenarios']),
            reason=None if ready == len(result['scenarios']) else 'incomplete_scenario_family')
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as error:
        result['reason'] = daily._reason(error, 'invalid_intraday_context')
    return result
