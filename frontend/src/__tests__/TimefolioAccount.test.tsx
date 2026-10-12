import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import TimefolioAccount, { TimefolioAccountView } from '../components/TimefolioAccount';
import { readTimefolio } from '../timefolio';
import useTimefolioAccount from '../useTimefolioAccount';
import { deferred, response } from './fixtures';
import { timefolio, waitingTimefolio } from './timefolio-fixtures';

const props = { loading: false, error: null, autoRefresh: true, onAutoRefresh: () => {}, onRefresh: () => {} };
function hidden(value: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value });
  fireEvent(document, new Event('visibilitychange'));
}

describe('Timefolio account display', () => {
  it('shows real NAV, ranking, holding values and unknown costs without inventing cash or total assets', () => {
    render(<TimefolioAccountView {...props} data={timefolio()}/>);
    expect(screen.getByText('1,012.5')).toBeTruthy();
    expect(screen.getByText('+2.5')).toBeTruthy();
    expect(screen.getByText('+0.25%')).toBeTruthy();
    expect(screen.getByText('기준가 (NAV)')).toBeTruthy();
    expect(screen.getByText('순위')).toBeTruthy();
    expect(screen.queryByText('예수금')).toBeNull();
    expect(screen.queryByText('총 평가금액')).toBeNull();
    const table = within(screen.getByRole('region', { name: '타임폴리오 보유 종목 상세' }));
    expect(table.getByRole('cell', { name: '2주' })).toBeTruthy();
    expect(table.getByRole('cell', { name: '77,750' })).toBeTruthy();
    expect(table.getByRole('cell', { name: '155,500' })).toBeTruthy();
    expect(table.getByRole('cell', { name: '1.23%' })).toBeTruthy();
    expect(table.getByRole('cell', { name: '—' })).toBeTruthy();
    expect(table.getByRole('cell', { name: /—\s*—/ })).toBeTruthy();
    expect(screen.getByText(/10:05:00 KST/).getAttribute('datetime')).toBe('2026-10-12T01:05:00.000Z');
  });

  it('keeps terminal completed orders distinct from confirmed fills and shows their source', () => {
    render(<TimefolioAccountView {...props} data={timefolio()}/>);
    const orders = within(screen.getByRole('region', { name: '타임폴리오 주문 상세' }));
    expect(orders.getByRole('columnheader', { name: '누적 체결수량' })).toBeTruthy();
    expect(orders.getByRole('cell', { name: /완료\s*order-one/ })).toBeTruthy();
    expect(orders.getByRole('cell', { name: /미확정\s*order-two/ })).toBeTruthy();
    expect(orders.getByRole('cell', { name: '자동 연동' })).toBeTruthy();
    expect(orders.getByRole('cell', { name: '기타' })).toBeTruthy();
    expect(orders.getByRole('cell', { name: '2주' })).toBeTruthy();
    expect(orders.queryByText('전량 체결')).toBeNull();
    const unknown = orders.getByRole('cell', { name: /SK하이닉스\s*000660/ }).closest('tr')!;
    expect(within(unknown).getAllByRole('cell', { name: '—' })).toHaveLength(2);
  });

  it('distinguishes first observation, verified empty results and cached failures', () => {
    const view = render(<TimefolioAccountView {...props} data={waitingTimefolio}/>);
    expect(screen.getByRole('status').textContent).toBe('첫 조회 대기');
    expect(screen.queryByText('보유 종목 없음')).toBeNull();
    view.rerender(<TimefolioAccountView {...props} data={timefolio({ holdings: [], orders: [] })}/>);
    expect(screen.getByText('보유 종목 없음')).toBeTruthy();
    expect(screen.getByText('주문 내역 없음')).toBeTruthy();
    view.rerender(<TimefolioAccountView {...props} data={timefolio({ stale: true, status: 'error', error: '로그인 필요' })}/>);
    expect(screen.getByRole('status').textContent).toBe('갱신 지연 · 이전 데이터');
    expect(screen.getByRole('alert').textContent).toBe('로그인 필요');
    expect(screen.getByText('1,012.5')).toBeTruthy();
    expect(screen.getByText(/10:05:00 KST/)).toBeTruthy();
  });

  it('rejects invalid identity, quantities, dates and order flags before displaying them', () => {
    const data = timefolio();
    expect(readTimefolio(data)).toEqual(data);
    expect(readTimefolio(waitingTimefolio)).toEqual(waitingTimefolio);
    expect(readTimefolio({ ...data, holdings: [{ ...data.holdings[0], name: null }],
      orders: [{ ...data.orders[0], name: null }] }).holdings[0].name).toBeNull();
    for (const invalid of [null, {}, { ...data, updated_at: 'today' }, { ...data, account: null },
      { ...data, summary: { ...data.summary, nav: 'NaN' } },
      { ...data, holdings: [{ ...data.holdings[0], quantity: undefined }] },
      { ...data, orders: [{ ...data.orders[0], system_owned: 'false' }] },
      { ...data, orders: [{ ...data.orders[0], side: 'other' }] }]) {
      expect(() => readTimefolio(invalid)).toThrow();
    }
  });
});

describe('Timefolio read-only requests', () => {
  it('loads and manually refreshes saved snapshots with GET only', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(timefolio()));
    vi.stubGlobal('fetch', fetchMock);
    render(<TimefolioAccount/>);
    await screen.findByText('1,012.5');
    await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, options] of fetchMock.mock.calls) {
      expect(url).toBe('/api/timefolio');
      expect(options).toMatchObject({ method: 'GET', headers: { 'X-KIS-Dashboard': '1' }, cache: 'no-store' });
      expect(options.body).toBeUndefined();
    }
  });

  it('retains successful observations after transport failure and clears them for an explicit empty account', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(timefolio())).mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response({ ...waitingTimefolio, status: 'error', error: '계좌 연결 변경' }));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useTimefolioAccount());
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data?.summary?.nav).toBe('1012.5');
    expect(result.current.data?.updated_at).toBe('2026-10-12T01:05:00Z');
    expect(result.current.error).toBeTruthy();
    await act(async () => { await result.current.refresh(); });
    expect(result.current.data?.account).toBeNull();
    expect(result.current.data?.summary).toBeNull();
    expect(result.current.data?.holdings).toEqual([]);
    expect(result.current.data?.orders).toEqual([]);
    expect(result.current.error).toBe('계좌 연결 변경');
  });

  it('does not relabel a newer polling time as the data observation time', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(timefolio())).mockResolvedValueOnce(response(timefolio({
      stale: true, status: 'error', error: '조회 실패', last_attempt_at: '2026-10-12T02:00:00Z',
    })));
    vi.stubGlobal('fetch', fetchMock);
    render(<TimefolioAccount/>);
    await screen.findByText('1,012.5');
    await userEvent.click(screen.getByRole('button', { name: '새로고침' }));
    expect(await screen.findByText('갱신 지연 · 이전 데이터')).toBeTruthy();
    expect(screen.getByText(/10:05:00 KST/)).toBeTruthy();
    expect(screen.queryByText(/11:00:00 KST/)).toBeNull();
  });

  it('keeps one automatic timer in StrictMode, pauses while hidden and cancels on unmount', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(response(timefolio()));
    vi.stubGlobal('fetch', fetchMock);
    const view = renderHook(() => useTimefolioAccount(), { wrapper: StrictMode });
    await act(async () => {});
    const initial = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 1);
    act(() => { hidden(true); });
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 1);
    await act(async () => { hidden(false); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 2);
    act(() => { view.result.current.setAutoRefresh(false); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 2);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('deduplicates requests and ignores an aborted hidden-tab response', async () => {
    const old = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(timefolio()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useTimefolioAccount());
    await act(async () => { await result.current.refresh(); await result.current.refresh(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    act(() => { hidden(true); });
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { hidden(false); });
    expect(result.current.data?.summary?.nav).toBe('1012.5');
    await act(async () => { old.resolve(response(waitingTimefolio)); });
    expect(result.current.data?.summary?.nav).toBe('1012.5');
  });

  it('times out stalled reads, ignores late results and aborts on page exit', async () => {
    vi.useFakeTimers();
    const old = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(old.promise);
    vi.stubGlobal('fetch', fetchMock);
    const { result, unmount } = renderHook(() => useTimefolioAccount());
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeTruthy();
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { old.resolve(response(timefolio())); });
    expect(result.current.data).toBeNull();
    const pending = deferred<Response>();
    fetchMock.mockReturnValue(pending.promise);
    act(() => { void result.current.refresh(); });
    unmount();
    expect(fetchMock.mock.calls.at(-1)![1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
