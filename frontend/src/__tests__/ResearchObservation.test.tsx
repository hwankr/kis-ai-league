import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { ResearchObservationPanel } from '../components/ResearchObservation';
import { readResearch } from '../research';
import useResearchObservation from '../useResearchObservation';
import { catalog, deferred, marketData, response, snapshot, tradeHistory } from './fixtures';
import { observation, research } from './research-fixtures';

const props = { loading: false, error: null, onRefresh: () => {}, onSelectSymbol: () => {} };
const empty = research({ counts: { signals: 0, prospective: 0, bootstrap: 0, late: 0, closed: 0, open: 0, unknown: 0 },
  observations: [], comparison: { signal_count: 0, control_count: 0, signal_mean: null, control_mean: null, edge: null,
    pending: true, descriptive_only: true, paired_days: 0, paired_groups: 0 } });

function visibility(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
  fireEvent(document, new Event('visibilitychange'));
}

describe('forward observation display', () => {
  it('separates bootstrap and late records from prospective counts and virtual returns', () => {
    render(<ResearchObservationPanel {...props} data={research({
      counts: { signals: 3, prospective: 1, bootstrap: 1, late: 1, closed: 1, open: 0, unknown: 0 },
      observations: [observation(), observation({ symbol: '000660', name: '초기 종목', classification: 'bootstrap' }),
        observation({ symbol: '035420', name: '지연 종목', classification: 'late' }),
        observation({ symbol: '035720', name: '무신호 종목', signal: false })],
    })}/>);
    const summary = screen.getByLabelText('전진 신호 집계');
    expect(summary.querySelector('dd')?.textContent).toBe('1');
    expect(screen.getByText('연구 중 · 미채택')).toBeDefined();
    expect(screen.getByText('자동 주문 없음')).toBeDefined();
    const table = screen.getByRole('table');
    expect(within(table).getAllByText('+2.50%')).toHaveLength(1);
    expect(within(table).getAllByText('평가 제외')).toHaveLength(2);
    expect(screen.queryByText('무신호 종목')).toBeNull();
  });

  it('renders decimal returns and null outcomes accurately and opens the selected stock', async () => {
    const onSelectSymbol = vi.fn();
    const unknown = observation({ symbol: '0126Z0', name: '미확정 종목', outcome: {
      ...observation().outcome, status: 'fill_unverifiable', returns: [{ slippage: 0.001, net_return: null }], reason: 'exit_fill_unverifiable',
    } });
    render(<ResearchObservationPanel {...props} onSelectSymbol={onSelectSymbol} data={research({ observations: [observation(), unknown] })}/>);
    expect(screen.getByText('+1.5%p')).toBeDefined();
    const row = screen.getByRole('button', { name: '미확정 종목 일봉 차트 보기' }).closest('tr')!;
    expect(within(row).getByText('체결 미확인')).toBeDefined();
    expect(within(row).getByText('청산 체결 미확인')).toBeDefined();
    expect(row.querySelector('.research-return')?.textContent).toBe('가상 수익률—');
    await userEvent.click(within(row).getByRole('button'));
    expect(onSelectSymbol).toHaveBeenCalledWith('0126Z0');
  });

  it('excludes an unverified signal until the API confirms its prospective classification', () => {
    const data = readResearch({ ...empty,
      counts: { ...empty.counts, signals: 1, timing_unverified: 1 },
      observations: [observation({ classification: 'timing_unverified', original_classification: 'timing_unverified',
        outcome: { ...observation().outcome, status: 'excluded', reason: 'timing_unverified' } })],
    });
    const { rerender } = render(<ResearchObservationPanel {...props} data={data}/>);
    expect(screen.getByText('시점 확인 대기 1')).toBeDefined();
    const table = screen.getByRole('table');
    expect(within(table).getByText('시점 확인 대기')).toBeDefined();
    expect(within(table).getByText('평가 제외')).toBeDefined();
    expect(within(table).queryByText('+2.50%')).toBeNull();
    expect(screen.getByLabelText('전진 신호 집계').querySelector('dd')?.textContent).toBe('0');
    expect(screen.queryByLabelText('완결된 신호일별 가상 비교')).toBeNull();
    const confirmed = readResearch(research({ counts: { ...research().counts, timing_unverified: 0 },
      observations: [observation({ original_classification: 'timing_unverified' })] }));
    rerender(<ResearchObservationPanel {...props} data={confirmed}/>);
    expect(screen.queryByText('시점 확인 대기')).toBeNull();
    expect(screen.queryByText('평가 제외')).toBeNull();
    expect(screen.getByLabelText('전진 신호 집계').querySelector('dd')?.textContent).toBe('1');
    expect(within(screen.getByRole('table')).getByText('+2.50%')).toBeDefined();
  });

  it('shows zero observations, pending collection and errors without inventing results', () => {
    const { rerender } = render(<ResearchObservationPanel {...props} data={empty}/>);
    expect(screen.getByText('전진 신호 없음')).toBeDefined();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByLabelText('완결된 신호일별 가상 비교')).toBeNull();
    rerender(<ResearchObservationPanel {...props} data={research({ observations: [] })}/>);
    expect(screen.getByText('최근 기록에 표시할 신호 없음')).toBeDefined();
    rerender(<ResearchObservationPanel {...props} data={{ ...empty, status: 'idle' }}/>);
    expect(screen.getByText('첫 관찰 수집 대기')).toBeDefined();
    rerender(<ResearchObservationPanel {...props} data={null} error="전진 관찰을 불러오지 못했습니다."/>);
    expect(screen.getByRole('alert').textContent).toContain('불러오지 못했습니다');
    expect(screen.queryByText('전진 신호 없음')).toBeNull();
    expect(screen.getByRole('button', { name: '다시 확인' })).toBeDefined();
  });

  it('paginates recent signals without losing the selected stock action', async () => {
    const rows = Array.from({ length: 11 }, (_, index) => observation({ symbol: String(index).padStart(6, '0'), name: `관찰${index}` }));
    render(<ResearchObservationPanel {...props} data={research({ observations: rows })}/>);
    expect(screen.getAllByRole('button', { name: /일봉 차트 보기/ })).toHaveLength(10);
    await userEvent.click(screen.getByRole('button', { name: '다음' }));
    expect(screen.getAllByRole('button', { name: /일봉 차트 보기/ })).toHaveLength(1);
    expect(screen.getByRole('button', { name: '관찰10 일봉 차트 보기' })).toBeDefined();
  });
});

describe('forward observation polling', () => {
  it('uses protected GET requests at 30 seconds and retains the last result through HTTP and collector failures', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(research())).mockResolvedValueOnce(response({}, 503))
      .mockResolvedValueOnce(response({ ...empty, status: 'error', error: '수집 실패' })).mockResolvedValue(response(empty));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useResearchObservation());
    await act(async () => {});
    expect(fetchMock.mock.calls[0]).toEqual(['/api/research', expect.objectContaining({ method: 'GET', cache: 'no-store',
      headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' } })]);
    await act(async () => { await vi.advanceTimersByTimeAsync(29_999); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.error).toContain('불러오지 못했습니다');
    expect(result.current.data?.observations).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(result.current.error).toBe('수집 실패');
    expect(result.current.data?.observations).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(result.current.error).toBeNull();
    expect(result.current.data?.observations).toHaveLength(0);
  });

  it('aborts hidden requests, ignores their late responses and refreshes immediately when visible', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(response(empty));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useResearchObservation());
    act(() => visibility(true));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => visibility(false));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => pending.resolve(response(research())));
    expect(result.current.data?.observations).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it('cleans up pending requests and polling on unmount', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockReturnValue(new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    const { unmount } = renderHook(() => useResearchObservation());
    unmount();
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); visibility(false); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('recovers after a request timeout even when the fetch implementation does not reject on abort', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(response(empty));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useResearchObservation());
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    await act(async () => pending.resolve(response(research())));
    expect(result.current.data?.observations).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it('survives StrictMode cleanup without duplicate polling or accepting a superseded request', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(response(empty));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useResearchObservation(), { wrapper: StrictMode });
    await act(async () => {});
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(response(research())));
    expect(result.current.data?.observations).toHaveLength(0);
    const initial = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 1);
  });
});

describe('research response integrity', () => {
  it('accepts an unavailable frozen version while keeping its error visible', () => {
    const unavailable = { ...empty, status: 'error' as const, version_id: null, frozen_at: null, error: '관찰 기준 파일 확인 필요' };
    expect(readResearch(unavailable)).toBe(unavailable);
    render(<ResearchObservationPanel {...props} data={unavailable} error={unavailable.error}/>);
    expect(screen.getByRole('alert').textContent).toBe(unavailable.error);
    expect(screen.queryByText('이전 결과 · 확인 필요')).toBeNull();
  });

  it.each([
    { ...research(), order_enabled: true },
    { ...research(), counts: { ...research().counts, timing_unverified: -1 } },
    { ...research(), observations: [observation({ classification: 'unexpected' as never })] },
    { ...research(), observations: [observation({ signal_date: '2026-02-30' })] },
    { ...research(), observations: [observation({ outcome: { ...observation().outcome, returns: [{ slippage: 0.001, net_return: NaN }] } })] },
  ])('rejects malformed or conflicting research data', invalid => {
    expect(() => readResearch(invalid)).toThrow();
  });
});

describe('candidate page integration', () => {
  function mockApp(researchResponse: Response) {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(url === '/api/research' ? researchResponse : response(url === '/api/candidates'
        ? { status: 'idle', error: null, updated_at: null, as_of: null, stale: false, progress: { completed: 0, total: 0 }, rows: [],
          universe: { status: 'verified', count: 0, as_of: '2026-10-05', checked_at: '2026-10-05T08:00:00Z', source_url: null, error: null } }
        : url === '/api/accounts' ? catalog : url === '/api/market' ? marketData() : url.startsWith('/api/trades?') ? tradeHistory(url)
          : url.startsWith('/api/chart?') ? { status: 'error', error: '차트 확인', symbol: '005930', interval: 'day', market: 'KRX',
            environment: 'paper', source: 'KIS', adjusted: true, updated_at: null, as_of: null, stale: false, bars: [] } : snapshot()));
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('leaves candidate collection available when the research endpoint is absent', async () => {
    window.history.replaceState(null, '', '/#/candidates');
    mockApp(response({}, 404));
    render(<App/>);
    await screen.findByText('전진 관찰을 불러오지 못했습니다.');
    await waitFor(() => expect(screen.getByRole('button', { name: '전체 조회' }).hasAttribute('disabled')).toBe(false));
    expect(screen.getByRole('heading', { name: '후보 종목 비교' })).toBeDefined();
  });

  it('opens the existing daily chart and unmounts observation polling on navigation', async () => {
    window.history.replaceState(null, '', '/#/candidates');
    const fetchMock = mockApp(response(research()));
    render(<App/>);
    await userEvent.click(await screen.findByRole('button', { name: '삼성전자 일봉 차트 보기' }));
    await waitFor(() => expect(window.location.hash).toBe('#/chart?symbol=005930&interval=day'));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === '/api/chart?symbol=005930&interval=day')).toBe(true));
    expect(screen.queryByRole('heading', { name: '전진 관찰' })).toBeNull();
  });
});
