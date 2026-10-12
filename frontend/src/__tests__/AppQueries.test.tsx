import { StrictMode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { navigate } from '../usePageNavigation';
import { catalog, deferred, element, marketData, response, snapshot, tradeHistory } from './fixtures';
import { experiments } from './experiment-fixtures';
import { timefolio } from './timefolio-fixtures';
import { research } from './research-fixtures';

const comparison = { status: 'idle', error: null, updated_at: null, as_of: null, stale: false,
  progress: { completed: 0, total: 0 }, rows: [],
  universe: { status: 'verified', as_of: '2026-09-21', checked_at: null, source_url: null, count: 0, error: null } };
const competition = {
  kis: { status: 'ok', phase: 'active', stale: false, error: null, updated_at: null, coverage: 'none',
    period: { start_date: '2026-10-12', end_date: '2026-11-20', as_of: '2026-10-12' },
    metrics: Object.fromEntries(['trading_days', 'gross_amount', 'symbols', 'api_orders'].map(key => [key,
      { value: null, target: key === 'gross_amount' ? 500000000 : 5, met: null, progress_pct: null }])) },
  timefolio: { status: 'waiting', stale: false, error: null, updated_at: null, turnover: null },
};
function payload(url: string) {
  const path = url.split('?')[0], params = new URL(url, 'http://localhost').searchParams;
  if (path === '/api/accounts') return catalog;
  if (path === '/api/account') return snapshot(params.get('account')!);
  if (path === '/api/trades') return tradeHistory(url);
  if (path === '/api/market') return marketData();
  if (path === '/api/candidates') return comparison;
  if (path === '/api/research') return research();
  if (path === '/api/experiments') return experiments();
  if (path === '/api/timefolio') return timefolio();
  if (path === '/api/competition') return competition;
  if (path === '/api/operations') return { dashboard: { status: 'ok', updated_at: null },
    mirror: { status: 'waiting_market', updated_at: null, reason: null, can_start: false, can_stop: true }, busy: false, error: null };
  if (path === '/api/chart') return { status: 'ok', symbol: params.get('symbol'), interval: params.get('interval'),
    name: '삼성전자', source: 'KIS', market: 'KRX', environment: 'paper', adjusted: true, stale: false,
    updated_at: '2026-10-12T07:00:00Z', as_of: '2026-10-12', error: null,
    bars: [{ time: '2026-10-12', open: '100', close: '100', high: '110', low: '90', volume: '1', partial: false }] };
  throw new Error(`Unexpected API: ${url}`);
}
function mockApi(override?: (url: string, options?: RequestInit) => Promise<Response> | Response | undefined) {
  const fetcher = vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input);
    return Promise.resolve(override?.(url, options) ?? response(payload(url)));
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}
const paths = (fetcher: ReturnType<typeof mockApi>) => fetcher.mock.calls.map(([url]) => String(url).split('?')[0]);
const calls = (fetcher: ReturnType<typeof mockApi>, path: string) => paths(fetcher).filter(value => value === path).length;
const go = async (page: string) => { await act(async () => { navigate(`#/${page}`); }); };
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const setHidden = async (value: boolean) => {
  await act(async () => {
    Object.defineProperty(document, 'hidden', { configurable: true, value });
    fireEvent(document, new Event('visibilitychange'));
  });
};

describe('visible page requests', () => {
  it.each([
    ['experiments', ['/api/accounts', '/api/operations', '/api/competition', '/api/experiments']],
    ['timefolio', ['/api/timefolio']],
    ['account', ['/api/accounts', '/api/account']],
    ['trades', ['/api/accounts', '/api/trades']],
    ['candidates', ['/api/candidates', '/api/research']],
    ['chart?symbol=005930&interval=day', ['/api/market', '/api/chart']],
  ])('requests only visible data on %s, including two refresh intervals', async (page, allowed) => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', `#/${page}`);
    const fetcher = mockApi();
    const view = render(<App/>);
    await act(async () => {});
    expect(new Set(paths(fetcher))).toEqual(new Set(allowed));
    await advance(60_000);
    expect(new Set(paths(fetcher))).toEqual(new Set(allowed));
    expect(fetcher.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true);
    expect(calls(fetcher, '/api/accounts')).toBe(allowed.includes('/api/accounts') ? 1 : 0);
    if (page === 'account') expect(calls(fetcher, '/api/account')).toBe(3);
    if (page === 'trades') expect(calls(fetcher, '/api/trades')).toBe(3);
    if (page.startsWith('chart')) expect(calls(fetcher, '/api/chart')).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('moves polling from balance to trades to Timefolio and back without losing selection', async () => {
    vi.useFakeTimers();
    localStorage.setItem('kis-dashboard-account', 'league');
    window.history.replaceState(null, '', '#/account');
    const fetcher = mockApi();
    render(<App/>);
    await act(async () => {});
    await go('trades');
    let marker = fetcher.mock.calls.length;
    await advance(30_000);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/trades']);
    await go('timefolio');
    marker = fetcher.mock.calls.length;
    await advance(60_000);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/timefolio', '/api/timefolio']);
    await go('account');
    expect(element('asset-history').dataset.accountId).toBe('league');
    expect(fetcher.mock.calls.filter(([url]) => String(url).startsWith('/api/account?')).every(([url]) => String(url).endsWith('account=league'))).toBe(true);
    marker = fetcher.mock.calls.length;
    await advance(30_000);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/account']);
  });

  it.each(['account', 'trades'])('aborts %s when leaving and ignores an obsolete response', async page => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', `#/${page}`);
    const old = deferred<Response>();
    let requestUrl = '', signal: AbortSignal | undefined;
    const endpoint = page === 'account' ? '/api/account?' : '/api/trades?';
    let count = 0;
    const fetcher = mockApi((url, options) => {
      if (url.startsWith(endpoint) && ++count === 1) {
        requestUrl = url; signal = options?.signal as AbortSignal;
        return old.promise;
      }
    });
    render(<App/>);
    await act(async () => {});
    expect(signal?.aborted).toBe(false);
    await go('timefolio');
    expect(signal?.aborted).toBe(true);
    const marker = fetcher.mock.calls.length;
    await act(async () => { old.resolve(response(payload(requestUrl))); });
    await advance(30_000);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/timefolio']);
    await go(page);
    expect(count).toBe(2);
    expect(page === 'account' ? element('connection-label').textContent : element('trade-history').getAttribute('aria-busy'))
      .toBe(page === 'account' ? '계좌 연결됨' : 'false');
  });

  it('does not chain a hidden balance request after a late account catalog response', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '#/account');
    const catalogRequest = deferred<Response>();
    let first = true;
    const fetcher = mockApi(url => {
      if (url === '/api/accounts' && first) { first = false; return catalogRequest.promise; }
    });
    render(<App/>);
    await go('timefolio');
    await act(async () => { catalogRequest.resolve(response(catalog)); });
    await advance(60_000);
    expect(calls(fetcher, '/api/account')).toBe(0);
    expect(calls(fetcher, '/api/trades')).toBe(0);
    await go('account');
    expect(calls(fetcher, '/api/account')).toBe(1);
  });

  it('pauses hidden-tab requests and respects the disabled refresh switch across pages', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '#/account');
    const fetcher = mockApi();
    const view = render(<StrictMode><App/></StrictMode>);
    await act(async () => {});
    await setHidden(true);
    let marker = fetcher.mock.calls.length;
    await advance(90_000);
    expect(fetcher.mock.calls.length).toBe(marker);
    await setHidden(false);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/account']);
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: '30초 자동 갱신' })); });
    marker = fetcher.mock.calls.length;
    await advance(60_000);
    expect(fetcher.mock.calls.length).toBe(marker);
    await go('trades');
    expect(paths(fetcher).at(-1)).toBe('/api/trades');
    marker = fetcher.mock.calls.length;
    await advance(60_000);
    expect(fetcher.mock.calls.length).toBe(marker);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '새로고침' })); });
    expect(paths(fetcher).slice(marker)).toEqual(['/api/accounts', '/api/trades']);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops candidate polling on leave and only fetches the chart selected in the URL', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '#/candidates');
    const fetcher = mockApi(url => url === '/api/candidates' ? response({ ...comparison, status: 'running' }) : undefined);
    render(<App/>);
    await act(async () => {});
    expect(calls(fetcher, '/api/candidates')).toBe(1);
    await advance(2_000);
    expect(calls(fetcher, '/api/candidates')).toBe(2);
    await go('chart?symbol=005930&interval=day');
    const marker = fetcher.mock.calls.length;
    await advance(30_000);
    expect(paths(fetcher).slice(marker)).toEqual(['/api/market']);
    expect(calls(fetcher, '/api/chart')).toBe(1);
    await go('candidates');
    expect(calls(fetcher, '/api/candidates')).toBe(3);
  });

  it('does not reload a completed chart on visibility changes or an invalid chart URL', async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '#/chart?symbol=005930&interval=day');
    const fetcher = mockApi();
    render(<App/>);
    await act(async () => {});
    expect(calls(fetcher, '/api/chart')).toBe(1);
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: '30초 자동 갱신' })); });
    const marker = fetcher.mock.calls.length;
    await setHidden(true);
    await setHidden(false);
    await go('chart?symbol=invalid&interval=day');
    await setHidden(true);
    await setHidden(false);
    await advance(60_000);
    expect(fetcher.mock.calls.length).toBe(marker);
    expect(calls(fetcher, '/api/chart')).toBe(1);
  });
});
