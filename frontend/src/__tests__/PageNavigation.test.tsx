import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import type { CandidateComparisonData, CandidateRow } from '../candidates';
import type { ChartInterval, StockChartData } from '../types';
import { catalog, element, marketData, response, snapshot, tradeHistory } from './fixtures';
import { experiments } from './experiment-fixtures';

function candidates(): CandidateComparisonData {
  const common = { status: 'ok' as const, error: null, as_of: '2026-10-02', close: '1000',
    return_5d_pct: '1', return_20d_pct: '2', excess_5d_pp: '1', excess_20d_pp: '2', turnover_ratio: '1' };
  const rows: CandidateRow[] = [{ ...common, symbol: '005930', name: '삼성전자', board: 'KOSPI',
    avg_turnover_20d: '10000000000', selection: { status: 'selected', rank: 1, score: '300', reasons: [] } },
  ...Array.from({ length: 30 }, (_, index): CandidateRow => ({ ...common,
    symbol: String(200 + index).padStart(6, '0'), name: `대기 종목 ${index + 1}`, board: 'KOSDAQ',
    avg_turnover_20d: String((index + 1) * 10_000_000_000),
    selection: { status: 'reserve', rank: null, score: String(200 - index), reasons: ['순위 대기'] } }))];
  return { status: 'complete', error: null, updated_at: '2026-10-04T04:00:00Z', as_of: '2026-10-02', stale: false,
    progress: { completed: rows.length, total: rows.length }, rows,
    universe: { status: 'verified', as_of: '2026-09-21', checked_at: '2026-10-04T04:00:00Z',
      source_url: 'https://www.truefriend.com/league', count: rows.length, error: null },
    screening: { policy_id: 'navigation-policy', status: 'ready', label: '관찰 기준', score_label: '선별 점수',
      score_unit: '', criteria: ['검증된 조회 자료'], checked_at: '2026-10-04T04:00:00Z',
      master_observed_at: '2026-10-04T03:00:00Z', counts: { selected: 1, reserve: 30, excluded: 0, unverified: 0 }, error: null } };
}

function chartData(url: string): StockChartData {
  const params = new URL(url, 'http://localhost').searchParams;
  const symbol = params.get('symbol')!;
  const interval = params.get('interval') as ChartInterval;
  return { status: 'ok', symbol, name: `종목 ${symbol}`, interval, market: 'KRX', environment: 'paper', source: 'KIS',
    adjusted: true, updated_at: '2026-10-04T04:00:00Z', as_of: '2026-10-02', stale: false, error: null,
    bars: [{ time: interval === 'day' ? '2026-10-02' : '2026-10-02T15:00:00+09:00',
      open: '1000', high: '1100', low: '900', close: '1050', volume: '100', partial: false }] };
}

function installFetch() {
  const comparison = candidates();
  const fetchMock = vi.fn((input: RequestInfo | URL, options?: RequestInit) => {
    const url = String(input);
    if (url === '/api/accounts') return Promise.resolve(response(catalog));
    if (url.startsWith('/api/account?')) return Promise.resolve(response(snapshot(new URL(url, 'http://localhost').searchParams.get('account')!)));
    if (url.startsWith('/api/trades?')) return Promise.resolve(response(tradeHistory(url)));
    if (url === '/api/market') return Promise.resolve(response(marketData()));
    if (url === '/api/experiments') return Promise.resolve(response(experiments()));
    if (url === '/api/candidates' && options?.method !== 'POST') return Promise.resolve(response(comparison));
    if (url.startsWith('/api/chart?')) return Promise.resolve(response(chartData(url)));
    throw new Error(`Unexpected navigation request: ${url}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function chartRequests(fetchMock: ReturnType<typeof installFetch>) {
  return fetchMock.mock.calls.map(([url]) => String(url)).filter(url => url.startsWith('/api/chart?'));
}

function menu() { return within(screen.getByRole('navigation', { name: '메인 메뉴' })); }

async function chooseFilter(label: string, option: string) {
  await userEvent.click(screen.getByRole('combobox', { name: label }));
  await userEvent.click(screen.getByRole('option', { name: option }));
}

async function ready() {
  await waitFor(() => expect(element('connection-label').textContent).toBe('계좌 연결됨'));
}

async function chartReady(symbol: string, interval: ChartInterval) {
  await waitFor(() => {
    expect(element('stock-chart').dataset.symbol).toBe(symbol);
    expect(element('stock-chart').dataset.interval).toBe(interval);
    expect(element('stock-chart').dataset.barCount).toBe('1');
    expect(element('stock-chart').getAttribute('aria-busy')).toBe('false');
  });
  expect((screen.getByLabelText('차트 종목코드') as HTMLInputElement).value).toBe(symbol);
  expect(screen.getByRole('button', { name: interval === 'day' ? '일봉' : interval === '5m' ? '5분' : '15분' })
    .getAttribute('aria-pressed')).toBe('true');
}

describe('page navigation', () => {
  it('exposes five menu destinations and only the active page content', async () => {
    installFetch();
    window.history.replaceState(null, '', '#/account');
    render(<App/>);
    await ready();
    const destinations = [
      ['내 계좌', 'account', '총 평가금액'], ['거래 내역', 'trades', '체결 내역'],
      ['후보 탐색', 'candidates', '후보 종목 비교'], ['시세·차트', 'chart', '종목 차트'],
      ['실험실', 'experiments', '모의 운용'],
    ] as const;
    expect(menu().getAllByRole('link')).toHaveLength(5);
    for (const [label, route, heading] of destinations) {
      await userEvent.click(menu().getByRole('link', { name: label }));
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(label));
      expect(window.location.hash).toBe(`#/${route}`);
      expect(menu().getAllByRole('link', { current: 'page' })).toHaveLength(1);
      expect(menu().getByRole('link', { name: label }).getAttribute('aria-current')).toBe('page');
      expect(document.title).toBe(`${label} · KIS AI League`);
      expect(screen.getByRole('heading', { level: 2, name: heading })).toBeTruthy();
      for (const [, other, otherHeading] of destinations) {
        if (other !== route) expect(screen.queryByRole('heading', { level: 2, name: otherHeading })).toBeNull();
      }
    }
  });

  it('retains candidate search, market, selection, sort and page after opening a chart and returning', async () => {
    const fetchMock = installFetch();
    window.history.replaceState(null, '', '#/candidates');
    render(<App/>);
    await screen.findByRole('button', { name: '삼성전자 차트 보기' });
    await chooseFilter('선별 상태', '순위 대기');
    await userEvent.type(screen.getByRole('searchbox', { name: '종목 검색' }), '대기');
    await chooseFilter('시장', 'KOSDAQ');
    await chooseFilter('정렬 지표', '20일 평균 거래대금');
    await userEvent.click(screen.getByRole('button', { name: '정렬 방향: 내림차순' }));
    await userEvent.click(screen.getByRole('button', { name: '다음' }));
    const firstRow = element('candidate-comparison').querySelector('tbody tr')!;
    const symbol = firstRow.getAttribute('data-symbol')!;
    const displayed = [...element('candidate-comparison').querySelectorAll('tbody tr')].map(row => row.getAttribute('data-symbol'));
    expect(displayed).toHaveLength(5);
    await userEvent.click(within(firstRow as HTMLElement).getByRole('button'));
    await chartReady(symbol, 'day');
    expect(window.location.hash).toBe(`#/chart?symbol=${symbol}&interval=day`);
    expect(chartRequests(fetchMock)).toEqual([`/api/chart?symbol=${symbol}&interval=day`]);
    await userEvent.click(menu().getByRole('link', { name: '후보 탐색' }));
    await screen.findByRole('heading', { level: 1, name: '후보 탐색' });
    expect((screen.getByRole('searchbox', { name: '종목 검색' }) as HTMLInputElement).value).toBe('대기');
    expect(screen.getByRole('combobox', { name: '시장' }).textContent).toBe('KOSDAQ');
    expect(screen.getByRole('combobox', { name: '선별 상태' }).textContent).toBe('순위 대기');
    expect(screen.getByRole('combobox', { name: '정렬 지표' }).textContent).toBe('20일 평균 거래대금');
    expect(screen.getByRole('button', { name: '정렬 방향: 오름차순' })).toBeTruthy();
    expect(screen.getByText('30종목 · 2 / 2페이지')).toBeTruthy();
    expect([...element('candidate-comparison').querySelectorAll('tbody tr')].map(row => row.getAttribute('data-symbol'))).toEqual(displayed);
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  });

  it('restores a direct chart URL on initial mount and a fresh app mount with one request each', async () => {
    const fetchMock = installFetch();
    const href = '#/chart?symbol=0126Z0&interval=15m';
    window.history.replaceState(null, '', href);
    const first = render(<App/>);
    await chartReady('0126Z0', '15m');
    expect(chartRequests(fetchMock)).toEqual(['/api/chart?symbol=0126Z0&interval=15m']);
    first.unmount();
    render(<App/>);
    await chartReady('0126Z0', '15m');
    expect(window.location.hash).toBe(href);
    expect(chartRequests(fetchMock)).toEqual(Array(2).fill('/api/chart?symbol=0126Z0&interval=15m'));
  });

  it('restores both symbol and interval on real history back/forward without duplicate chart requests', async () => {
    const fetchMock = installFetch();
    window.history.replaceState(null, '', '#/chart?symbol=005930&interval=day');
    render(<App/>);
    await chartReady('005930', 'day');
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '000660' } });
    await userEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
    await chartReady('000660', 'day');
    await userEvent.click(screen.getByRole('button', { name: '5분' }));
    await chartReady('000660', '5m');
    for (const [direction, symbol, interval] of [
      ['back', '000660', 'day'], ['back', '005930', 'day'],
      ['forward', '000660', 'day'], ['forward', '000660', '5m'],
    ] as const) {
      act(() => { window.history[direction](); });
      await waitFor(() => expect(window.location.hash).toBe(`#/chart?symbol=${symbol}&interval=${interval}`));
      await chartReady(symbol, interval);
    }
    expect(chartRequests(fetchMock)).toEqual([
      '/api/chart?symbol=005930&interval=day', '/api/chart?symbol=000660&interval=day', '/api/chart?symbol=000660&interval=5m',
      '/api/chart?symbol=000660&interval=day', '/api/chart?symbol=005930&interval=day',
      '/api/chart?symbol=000660&interval=day', '/api/chart?symbol=000660&interval=5m',
    ]);
  });

  it.each([
    ['#candidate-comparison', '#/candidates', '후보 탐색'],
    ['#stock-chart', '#/chart', '시세·차트'],
  ])('replaces legacy %s without adding a history entry', async (legacy, href, title) => {
    const fetchMock = installFetch();
    window.history.replaceState(null, '', legacy);
    const length = window.history.length;
    const replace = vi.spyOn(window.history, 'replaceState');
    const push = vi.spyOn(window.history, 'pushState');
    render(<App/>);
    await waitFor(() => expect(window.location.hash).toBe(href));
    await ready();
    expect(screen.getByRole('heading', { level: 1, name: title })).toBeTruthy();
    expect(replace).toHaveBeenCalledWith(null, '', href);
    expect(push).not.toHaveBeenCalled();
    expect(window.history.length).toBe(length);
    expect(chartRequests(fetchMock)).toEqual([]);
  });

  it('moves skip-link focus into the current page without replacing its chart URL', async () => {
    const fetchMock = installFetch();
    const href = '#/chart?symbol=005930&interval=5m';
    window.history.replaceState(null, '', href);
    render(<App/>);
    await chartReady('005930', '5m');
    const length = window.history.length;
    await userEvent.click(screen.getByRole('link', { name: '본문으로 바로가기' }));
    expect(document.activeElement).toBe(screen.getByRole('main'));
    expect(window.location.hash).toBe(href);
    expect(window.history.length).toBe(length);
    expect(chartRequests(fetchMock)).toEqual(['/api/chart?symbol=005930&interval=5m']);
  });

  it('shows malformed chart routes without requesting their chart data', async () => {
    const fetchMock = installFetch();
    for (const href of ['#/chart?symbol=bad&interval=day', '#/chart?symbol=005930&interval=week', '#/chart?symbol=&interval=5m']) {
      window.history.replaceState(null, '', href);
      const view = render(<App/>);
      await ready();
      expect(screen.getByRole('alert').textContent).toBe('차트 주소의 종목코드 또는 주기가 올바르지 않습니다.');
      expect(screen.getByRole('heading', { level: 1, name: '시세·차트' })).toBeTruthy();
      expect(chartRequests(fetchMock)).toEqual([]);
      view.unmount();
    }
  });
});
