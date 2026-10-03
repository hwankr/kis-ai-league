import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { defaultTradeRange, tradeRangeError } from '../useTradeHistory';
import { catalog, deferred, element, marketData, response, snapshot, trade, tradeHistory } from './fixtures';

function mockApi(trades: (url: string) => Response | Promise<Response>, accounts = () => response(catalog),
  balance = (url: string) => response(snapshot(new URL(url, 'http://localhost').searchParams.get('account') ?? 'practice'))) {
  const fetchMock = vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    void options;
    const url = String(input);
    return Promise.resolve(url === '/api/market' ? response(marketData()) : url === '/api/accounts' ? accounts() : url.startsWith('/api/trades?') ? trades(url) : balance(url));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function ready() {
  await waitFor(() => expect(element('trade-history').getAttribute('aria-busy')).toBe('false'));
  await waitFor(() => expect(element('connection-label').textContent).toBe('계좌 연결됨'));
}

async function refresh() {
  await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
  await waitFor(() => expect(element('refresh-button').getAttribute('aria-busy')).toBe('false'));
  await waitFor(() => expect(element('trade-history').getAttribute('aria-busy')).toBe('false'));
}

function queryRange(start: string, end: string) {
  fireEvent.change(screen.getByLabelText('시작일'), { target: { value: start } });
  fireEvent.change(screen.getByLabelText('종료일'), { target: { value: end } });
  fireEvent.submit(screen.getByRole('button', { name: '조회' }).closest('form')!);
}

describe('trade history', () => {
  it('uses 30 inclusive Seoul dates and validates 90 days and real dates', () => {
    expect(defaultTradeRange(new Date('2026-10-02T15:00:00Z'))).toEqual({ start: '2026-09-04', end: '2026-10-03' });
    expect(tradeRangeError({ start: '2026-02-30', end: '2026-03-01' })).toContain('날짜');
    expect(tradeRangeError({ start: '2025-01-01', end: '2025-03-31' })).toBeNull();
    expect(tradeRangeError({ start: '2025-01-01', end: '2025-04-01' })).toContain('90일');
    expect(tradeRangeError({ start: '2025-01-02', end: '2025-01-01' })).toContain('시작일');
  });

  it('distinguishes successful empty results and sends the read-only request headers', async () => {
    const fetchMock = mockApi(url => response(tradeHistory(url)));
    render(<App />);
    await ready();
    expect(screen.getByText('조회 기간에 체결 내역이 없습니다')).toBeTruthy();
    expect(element('trades-count').textContent).toBe('0');
    expect(document.getElementById('trades-error')).toBeNull();
    const request = fetchMock.mock.calls.find(([url]) => String(url).startsWith('/api/trades?'));
    expect(request?.[1]?.headers).toEqual({ Accept: 'application/json', 'X-KIS-Dashboard': '1' });
  });

  it('shows an initial trade error without changing successful balances or history', async () => {
    mockApi(url => response(tradeHistory(url, { status: 'error', error: '체결 조회 실패', updated_at: null }), 502));
    render(<App />);
    await ready();
    expect(screen.getByText('체결 조회 실패')).toBeTruthy();
    expect(screen.queryByText('조회 기간에 체결 내역이 없습니다')).toBeNull();
    expect(element('trades-count').textContent).toBe('—');
    expect(element('total-value').textContent).toBe('10,000,000');
    expect(element('asset-history').dataset.pointCount).toBe('1');
  });

  it('keeps balance refresh available while trade lookup is delayed', async () => {
    const slow = deferred<Response>();
    let requests = 0;
    mockApi(url => { requests += 1; return requests === 1 ? slow.promise : response(tradeHistory(url)); });
    render(<App />);
    await waitFor(() => expect(element('connection-label').textContent).toBe('계좌 연결됨'));
    expect(element('trade-history').getAttribute('aria-busy')).toBe('true');
    expect((element('refresh-button') as HTMLButtonElement).disabled).toBe(false);
    await refresh();
    expect(requests).toBe(2);
  });

  it('replaces the same order aggregate after partial fills and formats both sides', async () => {
    let requests = 0;
    mockApi(url => response(tradeHistory(url, { trades: [
      { ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')!, quantity: ++requests === 1 ? '2' : '5', price: requests === 1 ? '61000' : '61200.5' },
      { ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')!, order_id: '000124', side: 'sell', quantity: '1' },
    ], total_count: 2 })));
    render(<App />);
    await ready();
    expect(screen.getByRole('cell', { name: '2주' })).toBeTruthy();
    expect(screen.getByRole('cell', { name: '매수' })).toBeTruthy();
    expect(screen.getByRole('cell', { name: '매도' })).toBeTruthy();
    await refresh();
    expect(screen.queryByRole('cell', { name: '2주' })).toBeNull();
    expect(screen.getByRole('cell', { name: '5주' })).toBeTruthy();
    expect(screen.getByRole('cell', { name: /61,200.5/ })).toBeTruthy();
    expect(element('trade-history').dataset.rowCount).toBe('2');
    expect(screen.getByRole('columnheader', { name: '주문일' })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: '평균 체결가' })).toBeTruthy();
  });

  it('clears trades on account switch and ignores the late previous response', async () => {
    const oldRequest = deferred<Response>();
    const newRequest = deferred<Response>();
    let oldUrl = '';
    let newUrl = '';
    let calls = 0;
    mockApi(url => {
      if (url.includes('account=league')) { newUrl = url; return newRequest.promise; }
      if (++calls === 1) return response(tradeHistory(url, { trades: [{ ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')! }], total_count: 1 }));
      oldUrl = url;
      return oldRequest.promise;
    });
    render(<App />);
    await ready();
    await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
    await waitFor(() => expect(calls).toBe(2));
    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(screen.getByRole('option', { name: '대회 계좌' }));
    expect(element('trade-history').dataset.rowCount).toBe('0');
    await act(async () => oldRequest.resolve(response(tradeHistory(oldUrl, { trades: [{ ...trade, order_date: new URL(oldUrl, 'http://localhost').searchParams.get('end')! }], total_count: 1 }))));
    expect(element('trade-history').dataset.rowCount).toBe('0');
    await act(async () => newRequest.resolve(response(tradeHistory(newUrl))));
    await ready();
    expect(element('trade-history').dataset.accountId).toBe('league');
    expect(element('trades-count').textContent).toBe('0');
  });

  it('clears the old range, rejects its late result, and allows another query while loading', async () => {
    const oldRequest = deferred<Response>();
    let oldUrl = '';
    const seen: string[] = [];
    mockApi(url => {
      seen.push(url);
      if (url.includes('start=2025-01-01')) { oldUrl = url; return oldRequest.promise; }
      return response(tradeHistory(url));
    });
    render(<App />);
    await ready();
    queryRange('2025-01-01', '2025-01-31');
    expect(element('trade-history').dataset.rowCount).toBe('0');
    queryRange('2025-02-01', '2025-02-28');
    await waitFor(() => expect(element('trade-history').getAttribute('aria-busy')).toBe('false'));
    await act(async () => oldRequest.resolve(response(tradeHistory(oldUrl, { trades: [{ ...trade, order_date: '2025-01-02' }], total_count: 1 }))));
    expect(element('trade-history').dataset.startDate).toBe('2025-02-01');
    expect(element('trades-count').textContent).toBe('0');
    expect(seen).toHaveLength(3);
  });

  it.each(['identity', 'range', 'fresh-error'] as const)('clears cached trades for %s responses', async kind => {
    let calls = 0;
    mockApi(url => {
      const filled = tradeHistory(url, { trades: [{ ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')! }], total_count: 1 });
      if (++calls === 1) return response(filled);
      return response(kind === 'identity' ? { ...filled, account: { id: 'other', name: '다른 계좌' } }
        : kind === 'range' ? { ...filled, start_date: '1999-01-01' }
          : tradeHistory(url, { status: 'error', updated_at: null, stale: false, error: '새 계좌 인증 실패' }), 502);
    });
    render(<App />);
    await ready();
    expect(element('trade-history').dataset.rowCount).toBe('1');
    await refresh();
    expect(element('trade-history').dataset.rowCount).toBe('0');
    expect(document.getElementById('trades-error')).toBeTruthy();
  });

  it.each(['catalog-error', 'removed'] as const)('clears trades after %s', async kind => {
    let catalogs = 0;
    mockApi(url => response(tradeHistory(url, { trades: [{ ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')! }], total_count: 1 })),
      () => ++catalogs === 1 ? response(catalog) : kind === 'catalog-error' ? response({ error: '설정 오류' }, 500) : response({ ...catalog, accounts: [] }));
    render(<App />);
    await ready();
    await refresh();
    expect(element('trade-history').dataset.rowCount).toBe('0');
    expect((screen.getByRole('button', { name: '조회' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('retains a same-account cached result on failure and recovers from the next query', async () => {
    let calls = 0;
    mockApi(url => {
      const filled = tradeHistory(url, { trades: [{ ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')! }], total_count: 1 });
      return response(++calls === 2 ? { ...filled, status: 'error', stale: true, error: 'KIS 조회 실패' } : filled, calls === 2 ? 502 : 200);
    });
    render(<App />);
    await ready();
    await refresh();
    expect(element('trade-history').dataset.rowCount).toBe('1');
    expect(screen.getByText('KIS 조회 실패')).toBeTruthy();
    expect(screen.getByText('갱신 지연 · 이전 데이터')).toBeTruthy();
    await refresh();
    expect(document.getElementById('trades-error')).toBeNull();
    expect(element('trade-history').dataset.rowCount).toBe('1');
  });

  it('marks persisted overlapping-range rows as stale when the exact range has never synced', async () => {
    mockApi(url => response(tradeHistory(url, {
      status: 'error', stale: true, updated_at: null, error: 'KIS 조회 실패',
      trades: [{ ...trade, order_date: new URL(url, 'http://localhost').searchParams.get('end')! }], total_count: 1,
    }), 502));
    render(<App />);
    await ready();
    expect(element('trades-count').textContent).toBe('1');
    expect(screen.getByText('갱신 지연 · 이전 데이터')).toBeTruthy();
    expect(element('trade-history').dataset.rowCount).toBe('1');
  });

  it('blocks ranges above 90 days without making a request', async () => {
    const fetchMock = mockApi(url => response(tradeHistory(url)));
    render(<App />);
    await ready();
    const count = fetchMock.mock.calls.length;
    queryRange('2025-01-01', '2025-04-01');
    expect(screen.getByText('조회 기간은 최대 90일입니다.')).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBe(count);
  });

  it('shares the single automatic refresh timer and pauses when hidden or disabled', async () => {
    vi.useFakeTimers();
    let calls = 0;
    mockApi(url => { calls += 1; return response(tradeHistory(url)); });
    const view = render(<StrictMode><App /></StrictMode>);
    await act(async () => {});
    const initial = calls;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(calls).toBe(initial + 1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(calls).toBe(initial + 1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(calls).toBe(initial + 2);
    fireEvent.click(screen.getByRole('switch'));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(calls).toBe(initial + 2);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
