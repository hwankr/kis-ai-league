import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { catalog, deferred, element, marketData, quote, response, snapshot, tradeHistory } from './fixtures';

const savedMarket = () => marketData({ symbols: [quote.symbol], quotes: [quote], collector: {
  state: 'running', interval_seconds: 60, heartbeat_at: quote.observed_at, next_run_at: '2026-10-03T04:01:00Z', error: null,
} });

function mockApi(market = () => Promise.resolve(response(savedMarket())), accounts = () => response(catalog)) {
  const fetchMock = vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    void options;
    const url = String(input);
    if (url === '/api/market') return market();
    return Promise.resolve(url === '/api/accounts' ? accounts() : url.startsWith('/api/trades?') ? response(tradeHistory(url))
      : response(snapshot(new URL(url, 'http://localhost').searchParams.get('account') ?? 'practice')));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function ready() {
  await waitFor(() => expect(element('market-quotes').getAttribute('aria-busy')).toBe('false'));
  await waitFor(() => expect(element('refresh-button').getAttribute('aria-busy')).toBe('false'));
}

async function refresh() {
  await userEvent.click(screen.getByRole('button', { name: window.location.hash.startsWith('#/chart') ? '시세 새로고침' : '새로고침' }));
  await ready();
}

describe('market collection', () => {
  beforeEach(() => window.history.replaceState(null, '', '/#/chart'));

  it('loads saved quotes independently with values, query time and record count', async () => {
    const fetchMock = mockApi();
    render(<App />);
    await ready();
    const market = within(element('market-quotes'));
    expect(market.getByRole('columnheader', { name: '조회 시각' })).toBeTruthy();
    expect(market.getByRole('cell', { name: /61,200/ })).toBeTruthy();
    expect(market.getByText('-1.25%').classList.contains('loss')).toBe(true);
    expect(market.getByRole('cell', { name: /123,456주/ })).toBeTruthy();
    expect(market.getByRole('cell', { name: '2건' })).toBeTruthy();
    expect(market.getByText('수집 중')).toBeTruthy();
    expect(market.getByText(/2026.*13:00:00 KST/)).toBeTruthy();
    const request = fetchMock.mock.calls.find(([url]) => url === '/api/market');
    expect(request?.[1]?.headers).toEqual({ Accept: 'application/json', 'X-KIS-Dashboard': '1' });
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('/quote'))).toBe(true);
  });

  it('shows chart refresh as busy while account balances settle independently', async () => {
    const pending = deferred<Response>();
    mockApi(() => pending.promise);
    render(<App />);
    expect(screen.getByText('시세 수집 상태를 불러오는 중')).toBeTruthy();
    await waitFor(() => expect(element('connection-label').textContent).toBe('계좌 연결됨'));
    expect((element('refresh-button') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => pending.resolve(response(savedMarket())));
    await ready();
    expect(element('market-quotes').dataset.rowCount).toBe('1');
  });

  it('distinguishes an initial network failure from an empty watchlist and recovers', async () => {
    let calls = 0;
    mockApi(() => ++calls === 1 ? Promise.reject(new Error('offline')) : Promise.resolve(response(marketData())));
    render(<App />);
    await ready();
    expect(screen.getByText('시세 수집 상태를 불러오지 못했습니다.')).toBeTruthy();
    expect(screen.queryByText('등록된 관심 종목이 없습니다')).toBeNull();
    await refresh();
    expect(screen.getByText('등록된 관심 종목이 없습니다')).toBeTruthy();
    expect(screen.getByText('수집 전')).toBeTruthy();
    expect(document.getElementById('market-error')).toBeNull();
  });

  it('retains previous saved prices on a network failure and updates after recovery', async () => {
    let calls = 0;
    mockApi(() => ++calls === 2 ? Promise.reject(new Error('offline')) : Promise.resolve(response({ ...savedMarket(),
      quotes: [{ ...quote, price: calls > 2 ? '62000' : quote.price, total_count: calls > 2 ? 3 : 2 }],
    })));
    render(<App />);
    await ready();
    await refresh();
    expect(within(element('market-quotes')).getByRole('cell', { name: /61,200/ })).toBeTruthy();
    expect(element('collector-status').textContent).toBe('상태 확인 필요');
    await refresh();
    expect(within(element('market-quotes')).getByRole('cell', { name: /62,000/ })).toBeTruthy();
    expect(document.getElementById('market-error')).toBeNull();
  });

  it('displays a failed quote alongside successful quotes without removing saved values', async () => {
    mockApi(() => Promise.resolve(response({ ...savedMarket(), symbols: ['005930', '000660'], quotes: [
      { ...quote, error: '시세 조회 실패' }, { ...quote, symbol: '000660', price: null, volume: null,
        change_percent: null, cumulative_turnover: null, observed_at: null, total_count: 0, error: '응답 확인 필요' },
    ] })));
    render(<App />);
    await ready();
    expect(screen.getByText('갱신 실패 · 이전 시세')).toBeTruthy();
    expect(screen.getByText('수집 실패')).toBeTruthy();
    expect(element('market-quotes').dataset.rowCount).toBe('2');
    expect(within(element('market-quotes')).getByRole('cell', { name: /61,200/ })).toBeTruthy();
    expect(element('total-value').textContent).toBe('10,000,000');
  });

  it('keeps saved prices and the specific error when the local store becomes unavailable', async () => {
    let calls = 0;
    mockApi(() => Promise.resolve(response(++calls === 1 ? savedMarket() : marketData({
      symbols: ['005930'], status: 'error', error: '시세 기록을 읽지 못했습니다. 로컬 저장 파일을 확인하세요.',
    }), calls === 1 ? 200 : 502)));
    render(<App />);
    await ready();
    await refresh();
    expect(within(element('market-quotes')).getByRole('cell', { name: /61,200/ })).toBeTruthy();
    expect(element('market-error').textContent).toContain('로컬 저장 파일');
    expect(element('collector-status').textContent).toBe('상태 확인 필요');
  });

  it('displays first-collection placeholders without treating missing saved prices as an error', async () => {
    mockApi(() => Promise.resolve(response({ ...savedMarket(), quotes: [{ ...quote, price: null, volume: null,
      change_percent: null, cumulative_turnover: null, observed_at: null, last_attempt_at: null, total_count: 0 }],
    })));
    render(<App />);
    await ready();
    expect(screen.getByText('저장된 시세 없음')).toBeTruthy();
    expect(document.getElementById('market-error')).toBeNull();
    expect(element('market-quotes').dataset.rowCount).toBe('1');
  });

  it.each(['stopped', 'stale', 'not_started'] as const)('shows %s separately from refresh and empty states', async state => {
    const expected = { stopped: '수집 중지', stale: '수집 지연', not_started: '수집 전' };
    mockApi(() => Promise.resolve(response({ ...savedMarket(), collector: { ...savedMarket().collector, state } })));
    render(<App />);
    await ready();
    expect(element('collector-status').textContent).toBe(expected[state]);
    expect(element('market-quotes').dataset.rowCount).toBe('1');
  });

  it('keeps market data when the account catalog fails or account selection changes', async () => {
    let catalogs = 0;
    const fetchMock = mockApi(undefined, () => ++catalogs === 2 ? response({ error: '계좌 목록 오류' }, 500) : response(catalog));
    render(<App />);
    await ready();
    await userEvent.click(screen.getByRole('link', { name: '내 계좌' }));
    await ready();
    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(screen.getByRole('option', { name: '대회 계좌' }));
    await ready();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/market')).toHaveLength(1);
    await refresh();
    expect(element('error-notice').textContent).toContain('계좌 목록 오류');
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/market')).toHaveLength(1);
    await userEvent.click(screen.getByRole('link', { name: '시세·차트' }));
    await ready();
    expect(element('market-quotes').dataset.rowCount).toBe('1');
    expect(within(element('market-quotes')).getByRole('cell', { name: /61,200/ })).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/market')).toHaveLength(1);
  });

  it('loads quotes even when the initial account catalog is unavailable', async () => {
    mockApi(undefined, () => response({ error: '계좌 설정 오류' }, 500));
    render(<App />);
    await ready();
    expect(element('market-quotes').dataset.rowCount).toBe('1');
    expect(element('collector-status').textContent).toBe('수집 중');
  });

  it.each(['new-watchlist', 'invalid-new-watchlist', 'configuration-error'] as const)('clears removed-symbol data on %s', async kind => {
    let calls = 0;
    mockApi(() => Promise.resolve(response(++calls === 1 ? savedMarket()
      : kind === 'configuration-error' ? marketData({ status: 'error', error: '수집 설정 오류' })
        : { ...savedMarket(), symbols: ['000660'], quotes: [{ ...quote, symbol: kind === 'new-watchlist' ? '000660' : '005930' }] })));
    render(<App />);
    await ready();
    await refresh();
    expect(within(element('market-quotes')).queryByText('005930')).toBeNull();
    if (kind === 'new-watchlist') expect(within(element('market-quotes')).getByText('000660')).toBeTruthy();
    else expect(element('market-quotes').dataset.rowCount).toBe('0');
  });

  it('rejects duplicate symbols rather than displaying misleading rows', async () => {
    mockApi(() => Promise.resolve(response({ ...savedMarket(), symbols: ['005930', '000660'], quotes: [quote, quote] })));
    render(<App />);
    await ready();
    expect(element('market-quotes').dataset.rowCount).toBe('0');
    expect(element('market-error').textContent).toBe('시세 수집 상태를 불러오지 못했습니다.');
  });

  it('has one market refresh per interval in StrictMode and pauses on hide, toggle and unmount', async () => {
    vi.useFakeTimers();
    let calls = 0;
    mockApi(() => { calls += 1; return Promise.resolve(response(savedMarket())); });
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
