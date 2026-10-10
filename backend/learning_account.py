"""AI-owned capital, daily scorecard and durable loss protection in the existing DB."""
from copy import deepcopy
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


def execution_model(orders):
    """Freeze a small empirical paper-fill model before a trial; never refit its past."""
    terminal = [row for row in orders if row['status'] in {'filled', 'cancelled', 'rejected'}
                and not row.get('submission_skipped')][-100:]
    model = {'mode': 'observed_quotes', 'fill_ratio': 1., 'slippage_bps': 0,
             'stress_slippage_bps': 0, 'stress_fee_rate': float(FEE) + .001}
    for side in ('buy', 'sell'):
        rows = [row for row in terminal if row['side'] == side]
        if len(rows) >= 5:
            model[side + '_fill_ratio'] = sum(row['filled_quantity'] / row['quantity'] for row in rows) / len(rows)
    return model
