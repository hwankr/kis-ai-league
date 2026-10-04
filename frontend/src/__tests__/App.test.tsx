import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { catalog, deferred, element, history, marketData, response, snapshot, tradeHistory } from './fixtures';

function mockFetch(account: (url: string) => Response | Promise<Response>, accounts = () => response(catalog)) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/api/candidates') return Promise.resolve(response({ status: 'idle', error: null, updated_at: null,
      as_of: null, stale: false, progress: { completed: 0, total: 0 }, rows: [],
      universe: { status: 'unverified', as_of: null, checked_at: null, source_url: null, count: 0, error: null } }));
    return Promise.resolve(url === '/api/market' ? response(marketData()) : url === '/api/accounts' ? accounts() : url.startsWith('/api/trades?') ? response(tradeHistory(url)) : account(url));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function connected() {
  await waitFor(() => expect(element('connection-label').textContent).toBe('계좌 연결됨'));
}

async function refresh() {
  await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
  await waitFor(() => expect(element('refresh-button').getAttribute('aria-busy')).toBe('false'));
}

describe('account dashboard', () => {
  it('loads the default account, formats balances and displays an empty portfolio with its saved history', async () => {
    const fetchMock = mockFetch(() => response(snapshot()));
    render(<App />);
    await connected();
    expect(element('total-value').textContent).toBe('10,000,000');
    expect(element('cash').textContent).toBe('10,000,000');
    expect(element('unrealized-return').textContent).toBe('0.00%');
    expect(element('holdings-count').textContent).toBe('0');
    expect(element('empty-title').textContent).toBe('아직 보유한 주식이 없어요');
    expect(element('updated-at').textContent).toContain('KST');
    expect(element('asset-history').dataset.accountId).toBe('practice');
    expect(element('asset-history').dataset.pointCount).toBe('1');
    expect(element('error-notice').hidden).toBe(true);
    expect(fetchMock.mock.calls.map(([url]) => url).filter(url => String(url).startsWith('/api/account'))).toEqual(['/api/accounts', '/api/account?account=practice']);
  });

  it('renders holdings and signed gains, including a fractional quantity', async () => {
    mockFetch(() => response(snapshot('practice', {
      summary: { total_value: '11000000', cash: '5000000', securities_value: '6000000', unrealized_pnl: '120000', unrealized_return_pct: '2.04' },
      holdings: [{ symbol: '005930', name: '테스트 주식', quantity: '2.125', avg_price: '60000', price: '61000', market_value: '129625', purchase_amount: '127500', pnl: '2125', return_pct: '1.6667' }],
    })));
    render(<App />);
    await connected();
    expect(element('holdings-table-container').hidden).toBe(false);
    expect(screen.getByRole('cell', { name: /테스트 주식\s*005930/ })).toBeTruthy();
    expect(screen.getByRole('cell', { name: '2.125주' })).toBeTruthy();
    expect(screen.getByText('+2,125').classList.contains('gain')).toBe(true);
    expect(screen.getByText('+1.67%').classList.contains('gain')).toBe(true);
    expect(element('unrealized-pnl').textContent).toBe('+120,000');
  });

  it('clears the previous account immediately and ignores its late response after switching', async () => {
    const oldRequest = deferred<Response>();
    const newRequest = deferred<Response>();
    let practiceRequests = 0;
    mockFetch(url => {
      if (url.endsWith('account=league')) return newRequest.promise;
      return ++practiceRequests === 1 ? response(snapshot()) : oldRequest.promise;
    });
    render(<App />);
    await connected();
    await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
    await waitFor(() => expect(practiceRequests).toBe(2));
    await userEvent.click(screen.getByRole('combobox'));
    await userEvent.click(screen.getByRole('option', { name: '대회 계좌' }));
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
    expect(element('asset-history').dataset.accountId).toBe('league');
    expect(localStorage.getItem('kis-dashboard-account')).toBe('league');
    await act(async () => oldRequest.resolve(response(snapshot('practice', {
      summary: { total_value: '999999' },
    }))));
    expect(element('total-value').textContent).toBe('—');
    await act(async () => newRequest.resolve(response(snapshot('league', {
      summary: { total_value: '20000000', cash: '20000000' },
      history: { points: [{ observed_at: '2026-10-03T04:01:00Z', total_value: '20000000', cash: '20000000' }], total_count: 1, error: null },
    }))));
    await connected();
    expect(element('total-value').textContent).toBe('20,000,000');
    expect(element('asset-history').dataset.accountId).toBe('league');
    expect(element('history-chart').querySelector('circle')?.getAttribute('data-value')).toBe('20000000');
  });

  it.each([
    ['removed', '선택했던 계좌가 목록에 없어요'],
    ['pending', '이 계좌는 설정이 필요해요'],
  ])('does not substitute the default for saved selection %s', async (id, message) => {
    localStorage.setItem('kis-dashboard-account', id);
    const fetchMock = mockFetch(() => response(snapshot()));
    render(<App />);
    await waitFor(() => expect(element('error-notice').textContent).toContain(message));
    expect(fetchMock.mock.calls.map(([url]) => url).filter(url => url !== '/api/market' && url !== '/api/candidates')).toEqual(['/api/accounts']);
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
  });

  it.each([
    ['계좌 목록 확인 필요', response({ error: '설정을 읽을 수 없습니다' }, 500)],
    ['등록된 계좌 없음', response({ default_account: '', accounts: [] })],
  ])('distinguishes a failed catalog from an empty catalog: %s', async (label, catalogResponse) => {
    const fetchMock = mockFetch(() => response(snapshot()), () => catalogResponse);
    render(<App />);
    await waitFor(() => expect(element('refresh-button').getAttribute('aria-busy')).toBe('false'));
    expect(element('account-select-value').textContent).toBe(label);
    expect((element('account-select') as HTMLButtonElement).disabled).toBe(true);
    expect(fetchMock.mock.calls.map(([url]) => url).filter(url => url !== '/api/market' && url !== '/api/candidates')).toEqual(['/api/accounts']);
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
  });

  it('retains same-account values after a network failure, then recovers on refresh', async () => {
    let call = 0;
    mockFetch(() => {
      call += 1;
      if (call === 2) return Promise.reject(new Error('offline'));
      return response(snapshot('practice', { summary: { total_value: call === 1 ? '10000000' : '10000100' } }));
    });
    render(<App />);
    await connected();
    await refresh();
    expect(element('total-value').textContent).toBe('10,000,000');
    expect(element('asset-history').dataset.pointCount).toBe('1');
    expect(element('error-notice').textContent).toContain('이전 조회 데이터를 표시');
    expect(element('connection-label').textContent).toBe('갱신 지연 · 이전 데이터');
    await refresh();
    await connected();
    expect(element('total-value').textContent).toBe('10,000,100');
    expect(element('error-notice').hidden).toBe(true);
  });

  it('shows persisted history even if the first live snapshot fails', async () => {
    mockFetch(() => response(snapshot('practice', {
      status: 'error', updated_at: null, summary: {}, error: '조회 서버 오류', history,
    }), 502));
    render(<App />);
    await waitFor(() => expect(element('error-notice').textContent).toContain('조회 서버 오류'));
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('1');
    expect(element('history-chart').hasAttribute('hidden')).toBe(false);
    expect(element('error-notice').textContent).not.toContain('이전 조회 데이터를');
  });

  it('marks a cached error response as stale while retaining its values and observation count', async () => {
    mockFetch(() => response(snapshot('practice', {
      status: 'error', stale: true, error: '외부 조회 실패',
      summary: { total_value: '9500000', cash: '9500000' },
    }), 502));
    render(<App />);
    await waitFor(() => expect(element('error-notice').textContent).toContain('외부 조회 실패'));
    expect(element('total-value').textContent).toBe('9,500,000');
    expect(element('connection-label').textContent).toBe('갱신 지연 · 이전 데이터');
    expect(element('asset-history').dataset.observationCount).toBe('1');
    expect(element('error-notice').textContent).toContain('이전 조회 데이터를 표시');
  });

  it('clears existing data when the selected account disappears on catalog refresh', async () => {
    let catalogs = 0;
    const fetchMock = mockFetch(() => response(snapshot()), () => response(++catalogs === 1 ? catalog : {
      default_account: 'league', accounts: catalog.accounts.filter(account => account.id !== 'practice'),
    }));
    render(<App />);
    await connected();
    await refresh();
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
    expect(element('error-notice').textContent).toContain('선택했던 계좌가 목록에 없어요');
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/account?account=league');
  });

  it('clears balances and history when reloading account configuration fails', async () => {
    let catalogs = 0;
    mockFetch(() => response(snapshot()), () => ++catalogs === 1 ? response(catalog) : response({ error: '계좌 설정 오류' }, 500));
    render(<App />);
    await connected();
    await refresh();
    expect(element('error-notice').textContent).toContain('계좌 설정 오류');
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
    expect(element('history-chart').querySelectorAll('circle')).toHaveLength(0);
  });

  it('rejects an account response with a different identity', async () => {
    let calls = 0;
    mockFetch(() => response(snapshot(++calls === 1 ? 'practice' : 'league')));
    render(<App />);
    await connected();
    await refresh();
    expect(element('total-value').textContent).toBe('—');
    expect(element('asset-history').dataset.pointCount).toBe('0');
    expect(element('error-notice').hidden).toBe(false);
  });

  it('has one refresh timer in StrictMode, pauses while hidden, and cleans up on unmount', async () => {
    vi.useFakeTimers();
    let requests = 0;
    mockFetch(() => { requests += 1; return response(snapshot()); });
    const view = render(<StrictMode><App /></StrictMode>);
    await act(async () => {});
    const initialRequests = requests;
    expect(initialRequests).toBeGreaterThan(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(requests).toBe(initialRequests + 1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    fireEvent(document, new Event('visibilitychange'));
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(requests).toBe(initialRequests + 1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(requests).toBe(initialRequests + 2);
    fireEvent.click(screen.getByRole('switch'));
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(requests).toBe(initialRequests + 2);
    fireEvent.click(screen.getByRole('switch'));
    view.unmount();
    fireEvent(document, new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests).toBe(initialRequests + 2);
    expect(vi.getTimerCount()).toBe(0);
  });
});
