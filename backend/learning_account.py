"""AI-owned capital, daily scorecard and durable loss protection in the existing DB."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.experiment_store import encode

FEE, TAX = Decimal('0.000140527'), Decimal('0.002')
ACTIVE = {'submitting', 'unknown', 'submitted', 'partial', 'cancel_pending'}


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('invalid_account_value')
    return result


def owned_book(policy, positions, metrics, orders, prices, *, as_of, session_days=()):
    """Only confirmed project fills affect cash; external account cash is not income."""
    budget = number(policy['budget'])
    cash = budget + sum((number(row['realized_pnl']) for row in metrics), Decimal(0))
    lots = []
    for position in positions:
        symbol = position['symbol']
        if symbol not in prices or number(prices[symbol]) <= 0:
            raise ValueError('owned_price_missing')
        close, cost = number(prices[symbol]), number(position['cost'])
        cash -= cost
        lots.append({'symbol': symbol, 'strategy_id': position['strategy_id'],
            'quantity': position['quantity'], 'cost': str(cost),
            'entry_price': str(number(position['average_price']) / (1 + FEE)),
            'last_close': str(close), 'entry_date': position['entry_date'],
            'held_sessions': sum(position['entry_date'] < day <= as_of for day in session_days),
            'exit_pending': bool(position.get('exit_pending')),
            'exit_policy': deepcopy(position.get('exit_policy') or
                {'holding_sessions': 5, 'stop_loss_pct': 0, 'take_profit_pct': 0})})
    reserve = sum((number(order['limit_price']) * order.get('remaining_quantity', order['quantity']) * (1 + FEE)
        for order in orders if order['side'] == 'buy' and order['status'] in ACTIVE), Decimal(0))
    value = sum((number(row['last_close']) * row['quantity'] for row in lots), Decimal(0))
    # Compare marked NAV consistently net of estimated liquidation charges.
    equity = cash + value * (1 - FEE - TAX)
    return {'as_of': as_of, 'cash': str(cash), 'reserved_cash': str(reserve), 'positions': lots,
            'pending_entries': [], 'equity': str(equity), 'budget': str(budget),
            'unresolved_orders': sum(order['status'] in {'submitting', 'unknown', 'cancel_pending'} for order in orders),
            'pending_orders': sum(order['status'] in ACTIVE for order in orders)}


class LearningAccount:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS learning_daily '
                       '(fingerprint TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, '
                       'PRIMARY KEY(fingerprint,day))')

    def state(self, fingerprint):
        return self.store.setting('learning_account:' + fingerprint, {})

    def observe(self, policy, book, at, *, limit=15, daily=False, comparisons=None):
        fingerprint = policy.get('fingerprint')
        if not fingerprint:
            return None
        state = self.state(fingerprint)
        equity, budget = number(book['equity']), number(book['budget'])
        if budget <= 0 or not 0 < number(limit) <= 100:
            raise ValueError('invalid_risk_configuration')
        units = number(state.get('units', budget))
        flow = budget - number(state.get('budget', budget))
        if flow:
            # Mark the existing units before this flow, including newly realized P&L.
            flow_unit = (equity - flow) / units
            if flow_unit <= 0:
                raise ValueError('insolvent_capital_change')
            units += flow / flow_unit
        if units <= 0:
            raise ValueError('invalid_owned_capital')
        unit = equity / units
        peak = max(number(state.get('peak', 1)), unit)
        risk = dict(state.get('risk', {}))
        risk_peak = max(number(risk.get('peak', peak)), unit)
        drawdown = max(Decimal(0), (1 - unit / peak) * 100)
        risk_drawdown = max(Decimal(0), (1 - unit / risk_peak) * 100)
        if risk_drawdown >= number(limit) and not risk.get('active'):
            risk.update(active=True, triggered_at=at)
        risk.update(active=risk.get('active', False), peak=str(risk_peak), limit_pct=float(limit),
                    drawdown_pct=float(risk_drawdown))
        state.update(as_of=at, budget=str(budget), units=str(units), unit=str(unit), peak=str(peak),
                     equity=str(equity), cash=book['cash'], return_pct=float((unit - 1) * 100),
                     drawdown_pct=float(drawdown), max_drawdown_pct=max(float(drawdown), state.get('max_drawdown_pct', 0)),
                     risk=risk, pending_orders=book['pending_orders'], unresolved_orders=book['unresolved_orders'])
        if comparisons is not None:
            state['comparisons'] = comparisons
        key = 'learning_account:' + fingerprint
        with self.store.connect() as db:
            if daily:
                row = {'as_of': book['as_of'], 'equity': state['equity'], 'return_pct': state['return_pct'],
                       'drawdown_pct': state['drawdown_pct'], 'comparisons': state.get('comparisons'),
                       'risk_active': risk['active'], 'captured_at': at}
                db.execute('INSERT OR IGNORE INTO learning_daily VALUES (?,?,?)',
                           (fingerprint, book['as_of'], encode(row)))
            db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                       'WHERE settings.value != excluded.value', (key, encode(state)))
        return state

    def reset_protection(self, policy, at):
        state = self.state(policy['fingerprint'])
        if not state.get('risk', {}).get('active'):
            return
        # Explicit owner action only; keep cumulative NAV and historical drawdown.
        state['risk'].update(active=False, peak=state['unit'], reset_at=at, drawdown_pct=0)
        self.store.save_setting('learning_account:' + policy['fingerprint'], state)

    def snapshot(self, policy):
        identity = policy.get('fingerprint')
        state = self.state(identity) if identity else {}
        if not state:
            return None
        with self.store.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM learning_daily WHERE fingerprint=?', (identity,)).fetchone()[0]
        return {key: state.get(key) for key in ('as_of', 'equity', 'cash', 'return_pct', 'max_drawdown_pct',
                'risk', 'comparisons')} | {'observations': count, 'basis': 'project_confirmed_fills'}


def confirmed_execution_orders(orders):
    """Terminal orders with transmission evidence; retain zero-fill outcomes.

    Legacy rejection status alone cannot distinguish an authentication failure
    before transmission. Preserve ledger order; callers choose their own window.
    """
    terminal = []
    for row in orders:
        quantity, filled = row.get('quantity'), row.get('filled_quantity')
        if (row.get('status') not in {'filled', 'cancelled', 'rejected'}
                or row.get('submission_skipped') or row.get('request_sent') is False
                or row.get('side') not in {'buy', 'sell'}
                or type(quantity) is not int or quantity <= 0 or type(filled) is not int
                or not 0 <= filled <= quantity or row['status'] == 'filled' and filled != quantity):
            continue
        if not (filled > 0 or row.get('order_id') or row.get('accepted_at')
                or row.get('request_sent') is True or row.get('submission_skipped') is False):
            continue
        terminal.append(row)
    return terminal


def execution_profile(orders):
    """Freeze terminal paper-order evidence and explicitly labelled assumptions.

    Callers provide this account's past orders in ledger order. Zero-filled
    terminal orders count; unsubmitted/uncertain orders never do. No data means
    an unknown estimate, not a 100% fill estimate. Ranges describe the observed
    domain, not confidence bounds or proof of future liquidity. Volume is the
    latest positive one-minute bar, in shares, matching execution_quotes.volume.
    """
    terminal = confirmed_execution_orders(orders)[-100:]
    estimates, basis, support, prediction_basis, prediction_mae = {}, {}, {}, {}, {}
    def instant(value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError('naive_execution_time')
        return parsed
    for side in ('buy', 'sell'):
        rows = [row for row in terminal if row['side'] == side]
        basis[side + '_orders'] = len(rows)
        estimates[side + '_fill_ratio'] = (sum(row['filled_quantity'] / row['quantity'] for row in rows)
                                         / len(rows) if rows else None)
        contexts, errors = [], []
        for row in rows:
            try:
                price, volume = number(row['decision_price']), number(row['decision_volume'])
                observed = instant(row['decision_observed_at']).astimezone(timezone(timedelta(hours=9)))
                if price <= 0 or volume <= 0:
                    raise ValueError('invalid_execution_context')
                contexts.append((float(price), observed.hour * 60 + observed.minute, float(volume)))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                pass
            prediction = row.get('execution_prediction')
            if not isinstance(prediction, dict) or prediction.get('fill_ratio') is None:
                continue
            try:
                expected = number(prediction['fill_ratio'])
                created = instant(prediction['created_at'])
                if not 0 <= expected <= 1 or created > instant(row['created_at']):
                    continue
                errors.append(abs(float(expected) - row['filled_quantity'] / row['quantity']))
            except (KeyError, ValueError, TypeError, ArithmeticError):
                pass
        basis[side + '_context_orders'] = len(contexts)
        support[side] = ({key + suffix: function(values[index] for values in contexts)
                          for index, key in enumerate(('price', 'minute', 'volume'))
                          for suffix, function in (('_min', min), ('_max', max))} if contexts else None)
        prediction_basis[side] = len(errors)
        prediction_mae[side] = sum(errors) / len(errors) if errors else None
    base = {'mode': 'observed_quotes', 'slippage_bps': 0, 'stress_slippage_bps': 0,
            'stress_fee_rate': float(FEE) + .001}
    estimated = all(value is not None for value in estimates.values())
    scenarios = []
    if estimated:
        scenarios.append({'id': 'empirical', 'assumption': 'past_terminal_order_mean',
                          'model': {**base, **estimates}})
    scenarios.extend([
        {'id': 'partial_fill', 'assumption': 'fixed_half_fills_not_an_estimate',
         'model': {**base, 'fill_ratio': .5}},
        {'id': 'delayed_sell', 'assumption': 'full_fills_except_first_exit_delayed_one_session',
         'model': {**base, 'fill_ratio': 1., 'sell_delay_sessions': 1}},
        {'id': 'adverse_buy', 'assumption': 'ex_post_stress_omit_buys_with_close_above_decision_price',
         'model': {**base, 'fill_ratio': 1., 'adverse_buy': True}},
    ])
    return {'version': 1, 'status': 'estimated' if estimated else 'insufficient',
            'estimates': estimates, 'basis': basis, 'support': support, 'scenarios': scenarios,
            'prediction_basis': prediction_basis, 'prediction_mae': prediction_mae,
            'support_basis': 'observed_price_minute_one_minute_volume_ranges_only'}


def execution_model(orders):
    """Compatibility name: return the profile, never silently infer full fills."""
    return execution_profile(orders)
