import type { TimefolioData } from '../timefolio';

export function timefolio(overrides: Partial<TimefolioData> = {}): TimefolioData {
  return { status: 'ok', stale: false, error: null, updated_at: '2026-10-12T01:05:00Z',
    last_attempt_at: '2026-10-12T01:05:00Z', refresh_interval_seconds: 60,
    account: { name: '타임폴리오', contest: 'RFM 13회 대회', session_date: '2026-10-12' },
    summary: { nav: '1012.5', nav_change: '2.5', nav_change_pct: '0.25', rank: 12, participants: 100,
      holdings_value: '155500', holdings_pnl: null, holdings_return_pct: null },
    holdings: [{ symbol: '005930', name: '삼성전자', quantity: 2, avg_price: null, price: '77750',
      market_value: '155500', purchase_amount: null, pnl: null, return_pct: null, weight_pct: '1.23', change_pct: '0.5' }],
    orders: [{ order_id: 'order-one', symbol: '005930', name: '삼성전자', side: 'buy', status: 'completed',
      weight: '1.25', filled_quantity: 2, created_at: '2026-10-12T00:01:00Z', order_time: '09:01:00',
      average_price: null, verified: true, system_owned: true },
    { order_id: 'order-two', symbol: '000660', name: 'SK하이닉스', side: 'sell', status: 'unknown',
      weight: '2', filled_quantity: null, created_at: null, order_time: null,
      average_price: null, verified: false, system_owned: false }], ...overrides };
}

export const waitingTimefolio: TimefolioData = { status: 'waiting', stale: false, error: null, updated_at: null,
  last_attempt_at: null, refresh_interval_seconds: 60, account: null, summary: null, holdings: [], orders: [] };
