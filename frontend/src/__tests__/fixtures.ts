import type { AccountCatalog, AccountSnapshot, HistoryData, MarketData, MarketQuote, Trade, TradeHistory } from '../types';

export const catalog: AccountCatalog = {
  default_account: 'practice',
  accounts: [
    { id: 'practice', name: '일반 모의계좌', configured: true },
    { id: 'pending', name: '미설정 계좌', configured: false },
    { id: 'league', name: '대회 계좌', configured: true },
  ],
};

export const history: HistoryData = {
  points: [{ observed_at: '2026-10-03T04:00:00+00:00', total_value: '10000000', cash: '10000000' }],
  total_count: 1,
  error: null,
};

export function snapshot(id = 'practice', overrides: Partial<AccountSnapshot> = {}): AccountSnapshot {
  return {
    status: 'ok', environment: 'virtual', account: { id, name: catalog.accounts.find(account => account.id === id)?.name ?? id },
    updated_at: '2026-10-03T04:00:00+00:00', refresh_interval_seconds: 30,
    stale: false, error: null,
    summary: { total_value: '10000000', cash: '10000000', securities_value: '0', unrealized_pnl: '0', unrealized_return_pct: '0' },
    holdings: [], history,
    ...overrides,
  };
}

export function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

export const trade: Trade = {
  order_date: '2026-10-02', order_id: '000123', branch_id: '001', symbol: '005930', name: '체결 테스트 주식',
  side: 'buy', quantity: '2', price: '61000', amount: '122000', order_time: '100000',
};

export function tradeHistory(url: string, overrides: Partial<TradeHistory> = {}): TradeHistory {
  const query = new URL(url, 'http://localhost').searchParams;
  const id = query.get('account') ?? 'practice';
  return {
    status: 'ok', environment: 'paper', account: { id, name: catalog.accounts.find(account => account.id === id)?.name ?? id },
    start_date: query.get('start') ?? '2026-09-04', end_date: query.get('end') ?? '2026-10-03',
    trades: [], total_count: 0, updated_at: '2026-10-03T04:00:00+00:00', stale: false, error: null, ...overrides,
  };
}

export const quote: MarketQuote = {
  symbol: '005930', market: 'KRX', environment: 'paper', price: '61200', change_percent: '-1.25',
  volume: '123456', cumulative_turnover: '7555507200', observed_at: '2026-10-03T04:00:00+00:00',
  last_attempt_at: '2026-10-03T04:00:00+00:00', error: null, total_count: 2,
};

export function marketData(overrides: Partial<MarketData> = {}): MarketData {
  return { status: 'ok', error: null,
    collector: { state: 'not_started', heartbeat_at: null, interval_seconds: 60, next_run_at: null, error: null },
    symbols: [], quotes: [], ...overrides };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

export function element(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing element #${id}`);
  return found;
}
