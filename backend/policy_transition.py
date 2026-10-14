"""Pure next-session capacity at the last observed close, never an order forecast."""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import re

from backend.learning_policy import (FEE, TAX, KST, LEGACY_IDS, _initial_state,
                                     _strategies, canonical_entry_context, position_exit_due,
                                     signals, validate_policy)
from backend.portfolio_plan import plan_entries


VERSION = 'policy-transition-v1'
BASIS = 'reference_close_conditional_capacity'


def _money(value):
    return format(value, 'f')


def _amount(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('invalid_transition_amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid_transition_amount') from None
    if not result.is_finite() or result < 0 or positive and not result:
        raise ValueError('invalid_transition_amount')
    return result


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('invalid_transition_day')
    return value


def _count(value):
    if isinstance(value, list):
        return len(value)
    if type(value) is not int or value < 0:
        raise ValueError('invalid_order_count')
    return value


def _base():
    return {'version': VERSION, 'basis': BASIS, 'status': 'unavailable', 'reasons': [],
            'entry_count': None, 'gross_buy_amount': None, 'gross_sell_amount': None,
            'zero_entry_reason': 'unavailable', 'entries': [], 'exits': [],
            'assumptions': ['One next actual trading session; no future price or signal is generated.',
                            'Capacity uses the last completed close and current fees, not a predicted fill.',
                            'Live master, quote, broker buying power and account checks remain required.']}


def _snapshot(policy, frame, limits, context):
    if not isinstance(frame, dict) or not isinstance(context, dict) or not isinstance(limits, dict):
        raise ValueError('missing_transition_context')
    day = _day(frame.get('as_of'))
    if context.get('signal_day', day) != day:
        raise ValueError('context_day_mismatch')
    if 'latest_frame' in context and context['latest_frame'] != frame:
        raise ValueError('context_frame_mismatch')
    money = {key: _amount(limits.get(key), positive=True)
             for key in ('budget', 'order_cap', 'daily_buy_limit')}
    if not money['order_cap'] <= money['daily_buy_limit'] <= money['budget']:
        raise ValueError('invalid_transition_limits')
    if 'limits' in context and any(_amount(context['limits'].get(key)) != value for key, value in money.items()):
        raise ValueError('context_limits_mismatch')
    observed = datetime.fromisoformat(context.get('observed_at', ''))
    if observed.utcoffset() is None or observed < datetime.combine(date.fromisoformat(day), time(16), KST):
        raise ValueError('incomplete_transition_observation')
    frame_observed = datetime.fromisoformat(frame.get('observed_at', ''))
    if (frame_observed.utcoffset() is None or frame_observed > observed
            or frame_observed < datetime.combine(date.fromisoformat(day), time(16), KST)):
        raise ValueError('frame_observation_mismatch')
    if not isinstance(context.get('execution_model'), dict):
        raise ValueError('missing_execution_model')
    next_day = context.get('next_session')
    if next_day is not None:
        next_day = _day(next_day)
        if next_day <= day or observed >= datetime.combine(date.fromisoformat(next_day), time(9), KST):
            raise ValueError('invalid_next_session_assumption')
    days = context.get('session_days', frame.get('session_days'))
    if (not isinstance(days, list) or not days or any(_day(value) > day for value in days)
            or days != sorted(set(days)) or days[-1] != day):
        raise ValueError('invalid_observed_session_days')
    rows = frame.get('rows')
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError('invalid_transition_frame')
    if frame.get('missing_symbols'):
        raise ValueError('incomplete_transition_frame')
    by_symbol = {row['symbol']: row for row in rows}
    if len(by_symbol) != len(rows):
        raise ValueError('duplicate_frame_symbol')
    for row in rows:
        if (not isinstance(row.get('symbol'), str) or not re.fullmatch(r'[0-9A-Z]{6}', row['symbol'])
                or row.get('board') not in {'KOSPI', 'KOSDAQ'} or not isinstance(row.get('name'), str)
                or not isinstance(row.get('features'), dict)
                or type(row.get('learning_eligible')) is not bool):
            raise ValueError('invalid_transition_row')
        _amount(row.get('close'), positive=True)
    book = context.get('initial_state')
    required = {'as_of', 'budget', 'cash', 'reserved_cash', 'positions',
                'pending_orders', 'unresolved_orders', 'account_protection'}
    if not isinstance(book, dict) or not required <= book.keys():
        raise ValueError('incomplete_current_book')
    if book['as_of'] != day or _amount(book['budget']) != money['budget']:
        raise ValueError('current_book_context_mismatch')
    cash, reserved, positions, _ = _initial_state(book, money['budget'], day)
    for original in book['positions']:
        if 'held_sessions' not in original or 'exit_pending' not in original:
            raise ValueError('incomplete_position_state')
        symbol = original['symbol']
        if symbol not in by_symbol or _amount(by_symbol[symbol]['close']) != positions[symbol]['last_close']:
            raise ValueError('position_close_unavailable_or_changed')
        if original.get('entry_date') is not None:
            entry = _day(original['entry_date'])
            if entry > day:
                raise ValueError('invalid_entry_date')
            if entry in days and sum(entry < session <= day for session in days) != original['held_sessions']:
                raise ValueError('position_age_mismatch')
    pending, unresolved = _count(book['pending_orders']), _count(book['unresolved_orders'])
    reservations = book.get('reserved_entries', [])
    if not isinstance(reservations, list) or (reserved and not reservations):
        raise ValueError('incomplete_reservations')
    protection = book['account_protection']
    if (not isinstance(protection, dict) or type(protection.get('active')) is not bool
            or not {'units', 'peak', 'active', 'limit_pct'} <= protection.keys()):
        raise ValueError('incomplete_account_protection')
    units, peak = (_amount(protection[key], positive=True) for key in ('units', 'peak'))
    limit = _amount(protection['limit_pct'], positive=True)
    if limit > 100:
        raise ValueError('invalid_protection_limit')
    equity = cash + sum((position['last_close'] * position['quantity'] * (1 - FEE - TAX)
                         for position in positions.values()), Decimal(0))
    if 'equity' in book and abs(_amount(book['equity']) - equity) > Decimal('.000001'):
        raise ValueError('current_book_equity_mismatch')
    peak_now = max(peak, equity / units)
    drawdown = (1 - equity / units / peak_now) * 100
    protected = protection['active'] or drawdown >= limit
    blockers = context.get('account_blockers', [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) or not item for item in blockers):
        raise ValueError('invalid_account_blockers')
    eligibility = context.get('buy_eligibility')
    if eligibility is not None and (not isinstance(eligibility, dict)
                                   or any(type(value) is not bool for value in eligibility.values())):
        raise ValueError('invalid_current_eligibility')
    if policy['kind'] == 'legacy':
        baseline = frame.get('baseline_signals')
        if (not isinstance(baseline, list) or not baseline
                or {item.get('strategy_id') for item in baseline} != set(LEGACY_IDS)
                or len({(item.get('strategy_id'), item.get('symbol')) for item in baseline}) != len(baseline)
                or any(item.get('status') != 'ready' or item.get('action') not in {'buy', 'hold', 'avoid'}
                       for item in baseline)):
            raise ValueError('legacy_baseline_unavailable')
    return {'day': day, 'next_day': next_day, 'money': money, 'cash': cash, 'reserved': reserved,
            'positions': positions, 'reservations': reservations, 'pending': pending, 'unresolved': unresolved,
            'blockers': blockers, 'eligibility': eligibility, 'protected': protected,
            'frame_eligibility': {key: value.get('learning_eligible', True) is True for key, value in by_symbol.items()},
            'protection': {'units': _money(units), 'peak': _money(peak), 'observed_peak': _money(peak_now),
                           'active': protected, 'prior_active': protection['active'],
                           'limit_pct': float(limit), 'drawdown_pct': float(drawdown)}}


def _ordered(signals_list, policy, offset):
    if policy['kind'] != 'legacy':
        return signals_list
    identities = list(LEGACY_IDS)
    identities = identities[offset:] + identities[:offset]
    groups = {key: sorted((item for item in signals_list if item['strategy_id'] == key),
                          key=lambda item: item['symbol']) for key in identities}
    return [groups[key][index] for index in range(max((len(group) for group in groups.values()), default=0))
            for key in identities if index < len(groups[key])]


def _entries(policy, generated, state, removed, offset=0):
    money, cash = state['money'], state['cash']
    positions = {key: value for key, value in state['positions'].items() if key not in removed}
    for key in removed:
        lot = state['positions'][key]
        cash += lot['last_close'] * lot['quantity'] * (1 - FEE - TAX)
    held = set(positions) | {item['symbol'] for item in state['reservations']}
    weights = {item['id']: Decimal(str(item['weight'])) for item in _strategies(policy)}
    used = sum((item['cost'] for item in positions.values()), Decimal(0)) + state['reserved']
    floor = money['budget'] * Decimal(str(policy['cash_reserve']))
    strategy_used = {key: sum((item['cost'] for item in positions.values() if item['strategy_id'] == key), Decimal(0))
                     + sum((_amount(item['cost']) for item in state['reservations'] if item['strategy_id'] == key), Decimal(0))
                     for key in weights}
    slots = max(0, policy['max_positions'] - len(held)) if policy['kind'] == 'rules' else None
    capacity = {'available_cash': max(Decimal(0), cash - state['reserved'] - floor),
                'portfolio_remaining': max(Decimal(0), money['budget'] - floor - used),
                'daily_remaining': money['daily_buy_limit'], 'order_cap': money['order_cap'],
                'strategy_remaining': {key: max(Decimal(0), money['budget'] * weight - strategy_used[key])
                                       for key, weight in weights.items()},
                'strategy_weights': weights, 'max_new_positions': slots}
    ordered = _ordered(generated, policy, offset)
    eligibility = state['eligibility']
    current_eligibility = state['frame_eligibility'] if eligibility is None else eligibility
    reentry_blocked = removed if policy.get('no_same_day_reentry', False) else set()
    entry_excluded = held | reentry_blocked
    executable = [item for item in ordered if item['symbol'] not in entry_excluded
                  and current_eligibility.get(item['symbol']) is True]
    if state['protected']:
        plan = []
    elif policy.get('allocation_mode', 'legacy') != 'legacy':
        candidates = [{'symbol': item['symbol'], 'strategy_id': item['strategy_id'],
                       'price': str(item['reference_close']), 'quantity_cap': 10**12,
                       'rank': item.get('rank', index + 1)} for index, item in enumerate(executable)]
        plan = plan_entries(candidates, mode=policy['allocation_mode'], **capacity)
    else:
        # Keep the live legacy signal-count quota, ordering and skip behavior.
        counts, spent, plan = Counter(item['strategy_id'] for item in ordered), dict.fromkeys(weights, Decimal(0)), []
        available = min(capacity['available_cash'], capacity['portfolio_remaining'], capacity['daily_remaining'])
        for item in executable:
            symbol, strategy = item['symbol'], item['strategy_id']
            if symbol in held or slots is not None and len(plan) >= slots:
                continue
            price = _amount(item['reference_close'], positive=True)
            cap = min(money['order_cap'], money['daily_buy_limit'] * weights[strategy] / counts[strategy])
            amount = min(available, cap, capacity['strategy_remaining'][strategy] - spent[strategy],
                         money['daily_buy_limit'] * weights[strategy] - spent[strategy])
            quantity = max(0, int(amount / (price * (1 + FEE))))
            if quantity:
                cost = price * quantity * (1 + FEE)
                plan.append({'symbol': symbol, 'strategy_id': strategy, 'quantity': quantity,
                             'price': _money(price), 'estimated_cost': _money(cost)})
                available -= cost
                spent[strategy] += cost
                held.add(symbol)
    sources = {(item['symbol'], item['strategy_id']): item for item in generated}
    for item in plan:
        item['exit_policy'] = deepcopy(sources[(item['symbol'], item['strategy_id'])]['exit_policy'])
    gross = sum((_amount(item['price']) * item['quantity'] for item in plan), Decimal(0))
    fee_cost = sum((_amount(item['estimated_cost']) for item in plan), Decimal(0))
    reason = ('account_protection_active' if state['protected'] else 'no_signal' if not generated else
              'position_limit' if slots == 0 else 'already_held_or_reserved' if not any(
                  item['symbol'] not in held for item in ordered) and not plan else
              'same_day_reentry' if reentry_blocked and not any(
                  item['symbol'] not in entry_excluded for item in ordered) else
              'current_eligibility' if not executable else 'cash_or_allocation_limit') if not plan else None
    return {'entry_count': len(plan), 'gross_buy_amount': _money(gross), 'entry_cost': _money(fee_cost),
            'entries': plan, 'zero_entry_reason': reason, 'slots': slots,
            'cash_after_entries': _money(cash - fee_cost), 'cash_reserve': _money(floor),
            'capacity': {key: _money(value) for key, value in capacity.items() if isinstance(value, Decimal)}}


def transition_preview(policy, frame, limits, context):
    """Inspect one next actual session without changing the supplied frozen book.

    Accepts the award context or a comparison seed: initial_state, execution_model,
    observed_at, and optionally next_session/session_days/account_blockers/buy_eligibility.
    Book order counts and cumulative protection are required, never defaulted away.
    held_sessions=0 is the entry-day close; the next open completes one holding session.
    Main capacity assumes every due exit fills at the reference close. The separate
    without_exit_fills result retains all positions and all their reserved capital.
    """
    result = _base()
    try:
        policy = validate_policy(deepcopy(policy))
        state = _snapshot(policy, frame, limits, context)
        result.update(as_of=state['day'], next_session=state['next_day'],
                      next_session_basis='caller_supplied_assumption' if state['next_day'] else 'next_actual_session',
                      protection=state['protection'], held_position_count=len(state['positions']),
                      reserved_slot_count=len({item['symbol'] for item in state['reservations']}))
        reasons = (['unresolved_orders'] if state['unresolved'] else []) + (['pending_orders'] if state['pending'] else [])
        if state['blockers']:
            reasons.append('account_blockers')
        if state['reserved'] and not state['pending']:
            reasons.append('unsettled_reservations')
        if reasons:
            result['reasons'] = reasons
            return result
        if policy['kind'] == 'legacy':
            # Live legacy quotas count every frozen buy, before current eligibility.
            rows = {row['symbol']: row for row in frame['rows']}
            generated = [{**deepcopy(item), 'reference_close': rows[item['symbol']]['close'],
                          'exit_policy': {'holding_sessions': 5, 'stop_loss_pct': 0., 'take_profit_pct': 0.}}
                         for item in frame['baseline_signals'] if item['action'] == 'buy']
        else:
            # Select once against the original close book, before assuming any
            # due exit fills. Freed slots must not change the frozen top-N.
            entry_context = (canonical_entry_context(context['initial_state'])
                             if policy.get('held_before_top_n', False) else None)
            generated = signals(policy, frame, entry_context=entry_context)
            if entry_context is not None:
                result['entry_context'] = deepcopy(entry_context)
        due = []
        for symbol, lot in state['positions'].items():
            age = lot['held_sessions'] + 1
            if state['protected'] or lot['exit_pending'] or position_exit_due(lot['exit_policy'], symbol=symbol,
                    held_sessions=age, entry_price=lot['entry_price'], last_close=lot['last_close'],
                    quantity=lot['quantity'], cost=lot['cost'], frame=frame):
                due.append({'symbol': symbol, 'quantity': lot['quantity'], 'reference_close': _money(lot['last_close']),
                            'completed_holding_sessions': age, 'exit_policy': deepcopy(lot['exit_policy']),
                            'reason': 'account_protection' if state['protected'] else
                                      'existing_exit_pending' if lot['exit_pending'] else 'entry_exit_rule'})
        removed = {item['symbol'] for item in due}
        offsets = ([date.fromisoformat(state['next_day']).toordinal() % len(LEGACY_IDS)] if state['next_day']
                   else list(range(len(LEGACY_IDS)))) if policy['kind'] == 'legacy' else [0]
        scenarios = [_entries(policy, generated, state, removed, offset) for offset in offsets]
        primary = min(scenarios, key=lambda value: (_amount(value['gross_buy_amount']), value['entry_count']))
        without = [_entries(policy, generated, state, set(), offset) for offset in offsets]
        conservative = min(without, key=lambda value: (_amount(value['gross_buy_amount']), value['entry_count']))
        result.update(status='ready', reasons=[], **primary, exits=due,
                      exit_count=len(due), gross_sell_amount=_money(sum((state['positions'][key]['last_close']
                          * state['positions'][key]['quantity'] for key in removed), Decimal(0))),
                      slots_before=conservative['slots'], slots_after=primary['slots'],
                      retained_positions=[{'symbol': symbol, 'quantity': lot['quantity'],
                          'exit_policy': deepcopy(lot['exit_policy'])} for symbol, lot in state['positions'].items()
                          if symbol not in removed], signal_count=len(generated),
                      requires_exit_fills=bool(due), without_exit_fills=conservative)
        if due:
            result['assumptions'].append('Due exits fill completely at the reference close before conditional entries.')
        if policy['kind'] == 'legacy' and state['next_day'] is None:
            result['legacy_priority_range'] = {'rotations': len(offsets),
                'minimum_gross_buy_amount': primary['gross_buy_amount'],
                'maximum_gross_buy_amount': max(scenarios, key=lambda value: _amount(value['gross_buy_amount']))['gross_buy_amount'],
                'minimum_entry_count': min(value['entry_count'] for value in scenarios),
                'maximum_entry_count': max(value['entry_count'] for value in scenarios)}
            result['assumptions'].append('Legacy date rotation is unknown; the smallest capacity across all four rotations is shown.')
        return result
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        reason = str(error)
        result['reasons'] = [reason if reason and all(char.islower() or char == '_' for char in reason)
                             else 'invalid_transition_snapshot']
        return result
