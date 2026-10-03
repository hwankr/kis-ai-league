import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import StockChart from '../components/StockChart';
import useStockChart from '../useStockChart';
import type { StockChartData } from '../types';
import { catalog, deferred, element, marketData, quote, response, snapshot, tradeHistory } from './fixtures';

function chartData(overrides: Partial<StockChartData> = {}): StockChartData {
  return {
    status: 'ok', symbol: '005930', name: '삼성전자', market: 'KRX', environment: 'paper', source: 'KIS',
    interval: 'day', adjusted: true, updated_at: '2026-10-03T04:00:00Z', as_of: '2026-10-02', stale: false, error: null,
    bars: [
      { time: '2026-10-01', open: '61000', high: '62000', low: '60000', close: '61500', volume: '1000000', partial: false },
      { time: '2026-10-02', open: '61500', high: '62000', low: '60500', close: '61000', volume: '800000', partial: false },
    ], ...overrides,
  };
}

function View() { return <StockChart chart={useStockChart()}/>; }
async function query() {
  fireEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
  await waitFor(() => expect(element('stock-chart').getAttribute('aria-busy')).toBe('false'));
}

describe('stock chart', () => {
  it('does not request or poll until explicitly queried, including when interval changes', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<StrictMode><View/></StrictMode>);
    expect(screen.getByLabelText('차트 종목코드').getAttribute('value')).toBe('005930');
    fireEvent.click(screen.getByRole('button', { name: '5분' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(element('stock-chart').dataset.interval).toBe('5m');
    expect(screen.getByText('종목을 선택하거나 코드를 입력해 조회하세요')).toBeTruthy();
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('loads daily candles and volume with the actual prior trading date and navigable OHLC', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(chartData()));
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    await query();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chart?symbol=005930&interval=day');
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ Accept: 'application/json', 'X-KIS-Dashboard': '1' });
    expect(element('stock-chart').dataset.barCount).toBe('2');
    expect(element('stock-candles').querySelectorAll('.stock-volume-bar')).toHaveLength(2);
    expect(element('stock-candles').querySelectorAll('.candle-up')).toHaveLength(1);
    expect(element('stock-candles').querySelectorAll('.candle-down')).toHaveLength(1);
    expect(element('stock-candles').innerHTML).not.toMatch(/NaN|Infinity/);
    expect(screen.getByText(/마지막 거래일/).textContent).toContain('2026-10-02');
    expect(screen.getByText(/2026.*13:00:00 KST/)).toBeTruthy();
    const slider = screen.getByRole('slider', { name: '삼성전자 일봉 차트' });
    expect(slider.getAttribute('aria-valuetext')).toContain('2026-10-02 KST');
    expect(slider.getAttribute('aria-valuetext')).toContain('거래량 800,000주');
    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    expect(slider.getAttribute('aria-valuetext')).toContain('2026-10-01 KST');
    expect(element('stock-chart-selection').textContent).toContain('종가61,500원');
    fireEvent.keyDown(slider, { key: 'End' });
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(slider.getAttribute('aria-valuenow')).toBe('2');
    fireEvent.keyDown(slider, { key: 'Home' });
    expect(slider.getAttribute('aria-valuenow')).toBe('1');
  });

  it('selects candles by pointer position and exposes a partial minute bar', async () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(chartData({ interval: '5m', adjusted: false, bars: [
      { ...chartData().bars[0], time: '2026-10-02T09:00:00+09:00' },
      { ...chartData().bars[1], time: '2026-10-02T09:05:00+09:00', partial: true },
    ] }))));
    render(<View/>);
    fireEvent.click(screen.getByRole('button', { name: '5분' }));
    await query();
    const slider = screen.getByRole('slider');
    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({ width: 240, left: 0 } as DOMRect);
    expect(screen.getByText('진행 중')).toBeTruthy();
    expect(slider.getAttribute('aria-valuetext')).toContain('09:05 KST');
    fireEvent.pointerDown(slider, { clientX: 80 });
    expect(slider.getAttribute('aria-valuetext')).toContain('09:00 KST');
    expect(document.activeElement).toBe(slider);
  });

  it('keeps same-query data marked stale after failure and recovers on manual refresh', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(chartData()))
      .mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(chartData({ updated_at: '2026-10-03T04:05:00Z' })));
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    await query();
    fireEvent.click(screen.getByRole('button', { name: '차트 새로고침' }));
    await waitFor(() => expect(element('stock-chart').getAttribute('aria-busy')).toBe('false'));
    expect(fetchMock.mock.calls[1][0]).toBe('/api/chart?symbol=005930&interval=day&refresh=1');
    expect(element('stock-chart').dataset.barCount).toBe('2');
    expect(screen.getByText('이전 조회 데이터')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('다시 조회');
    fireEvent.click(screen.getByRole('button', { name: '차트 새로고침' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(screen.queryByText('이전 조회 데이터')).toBeNull();
    expect(screen.getByText(/13:05:00 KST/)).toBeTruthy();
    expect(fetchMock.mock.calls[2][0]).toBe('/api/chart?symbol=005930&interval=day&refresh=1');
  });

  it('immediately restores a revisited symbol while updating, and retains that cached chart on failure', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(chartData()))
      .mockResolvedValueOnce(response(chartData({ symbol: '000660', name: 'SK하이닉스' }))).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    await query();
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '000660' } });
    await query();
    expect(screen.getByRole('slider', { name: 'SK하이닉스 일봉 차트' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '005930' } });
    fireEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
    expect(screen.getByRole('slider', { name: '삼성전자 일봉 차트' })).toBeTruthy();
    expect(screen.queryByText('SK하이닉스')).toBeNull();
    expect(screen.getByText('갱신 중')).toBeTruthy();
    expect(screen.getByText(/13:00:00 KST/)).toBeTruthy();
    expect(fetchMock.mock.calls[2][0]).toBe('/api/chart?symbol=005930&interval=day');
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.getByRole('slider', { name: '삼성전자 일봉 차트' })).toBeTruthy();
    expect(screen.getByText('이전 조회 데이터')).toBeTruthy();
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByText(/13:00:00 KST/)).toBeTruthy();
  });

  it('restores each interval independently without relabeling daily bars as minutes', async () => {
    const pending = deferred<Response>();
    const minutes = chartData({ interval: '5m', adjusted: false, bars: [
      { ...chartData().bars[0], time: '2026-10-02T09:00:00+09:00' },
    ] });
    const fetchMock = vi.fn().mockResolvedValueOnce(response(minutes)).mockResolvedValueOnce(response(chartData())).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    fireEvent.click(screen.getByRole('button', { name: '5분' }));
    await query();
    fireEvent.click(screen.getByRole('button', { name: '일봉' }));
    await waitFor(() => expect(screen.getByRole('slider', { name: '삼성전자 일봉 차트' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '5분' }));
    expect(screen.getByRole('slider', { name: '삼성전자 5분 차트' }).getAttribute('aria-valuetext')).toContain('09:00 KST');
    expect(element('stock-chart').dataset.barCount).toBe('1');
    expect(screen.getByText('갱신 중')).toBeTruthy();
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('refresh='))).toBe(true);
    await act(async () => pending.resolve(response(minutes)));
    expect(screen.queryByText('갱신 중')).toBeNull();
  });

  it('keeps the 32 most recently viewed charts and fetches an evicted symbol without showing another chart', async () => {
    const fetchMock = vi.fn((url: string) => Promise.resolve(response(chartData({
      symbol: new URL(url, 'http://localhost').searchParams.get('symbol')!,
    }))));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useStockChart());
    for (let index = 0; index < 32; index++) {
      await act(async () => { await result.current.query(String(100000 + index), 'day'); });
    }
    await act(async () => { await result.current.query('100000', 'day'); });
    await act(async () => { await result.current.query('100032', 'day'); });
    const pending = deferred<Response>();
    fetchMock.mockImplementation(() => pending.promise);
    act(() => { void result.current.query('100000', 'day'); });
    expect(result.current.data?.symbol).toBe('100000');
    expect(result.current.loading).toBe(true);
    act(() => { void result.current.query('100001', 'day'); });
    expect(result.current.data).toBeNull();
    await act(async () => pending.resolve(response(chartData({ symbol: '100001' }))));
    expect(result.current.data?.symbol).toBe('100001');
  });

  it('does not let a superseded response overwrite the cache or another symbol', async () => {
    const oldUpdate = deferred<Response>();
    const nextUpdate = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(chartData()))
      .mockResolvedValueOnce(response(chartData({ symbol: '000660', name: 'SK하이닉스' })))
      .mockReturnValueOnce(oldUpdate.promise).mockResolvedValueOnce(response(chartData({ symbol: '000660', name: 'SK하이닉스' })))
      .mockReturnValueOnce(nextUpdate.promise);
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useStockChart());
    await act(async () => { await result.current.query('005930', 'day'); });
    await act(async () => { await result.current.query('000660', 'day'); });
    act(() => { void result.current.query('005930', 'day'); });
    expect(result.current.data?.symbol).toBe('005930');
    await act(async () => { await result.current.query('000660', 'day'); });
    expect(fetchMock.mock.calls[2][1].signal.aborted).toBe(true);
    await act(async () => oldUpdate.resolve(response(chartData({ updated_at: '2026-10-03T04:05:00Z' }))));
    expect(result.current.data?.symbol).toBe('000660');
    act(() => { void result.current.query('005930', 'day'); });
    expect(result.current.data?.updated_at).toBe('2026-10-03T04:00:00Z');
    await act(async () => nextUpdate.resolve(response(chartData())));
  });

  it('shows server-cached bars on an initial 503, but no former bars for another symbol', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(chartData({ status: 'error', stale: true, error: 'KIS 조회 지연' }), 503))
      .mockResolvedValue(response(chartData({ status: 'error', symbol: '000660', name: 'SK하이닉스', bars: [], as_of: null,
        updated_at: null, error: '자료 없음' }), 503));
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    await query();
    expect(screen.getByText('KIS 조회 지연')).toBeTruthy();
    expect(screen.getByText('이전 조회 데이터')).toBeTruthy();
    expect(element('stock-chart').dataset.barCount).toBe('2');
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '000660' } });
    await query();
    expect(screen.queryByRole('slider')).toBeNull();
    expect(screen.queryByText('삼성전자')).toBeNull();
    expect(element('stock-chart').dataset.barCount).toBe('0');
    expect(screen.getByText('자료 없음')).toBeTruthy();
  });

  it('aborts and ignores an older symbol request even when it resolves last', async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    fireEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '000660' } });
    fireEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => second.resolve(response(chartData({ symbol: '000660', name: 'SK하이닉스' }))));
    expect(screen.getByRole('slider', { name: 'SK하이닉스 일봉 차트' })).toBeTruthy();
    await act(async () => first.resolve(response(chartData())));
    expect(screen.queryByText('삼성전자')).toBeNull();
    expect(element('stock-chart').dataset.symbol).toBe('000660');
  });

  it('clears daily data immediately when switching to minutes and keeps it cleared on failure', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(chartData())).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    render(<View/>);
    await query();
    fireEvent.click(screen.getByRole('button', { name: '15분' }));
    expect(screen.queryByRole('slider')).toBeNull();
    expect(element('stock-chart').dataset.interval).toBe('15m');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/chart?symbol=005930&interval=15m');
    await act(async () => pending.reject(new Error('offline')));
    expect(screen.queryByRole('slider')).toBeNull();
    expect(screen.queryByText('이전 조회 데이터')).toBeNull();
  });

  it.each(['invalid-json', 'wrong-symbol', 'invalid-ohlc'])('rejects %s while retaining only an existing matching chart', async kind => {
    const badResponse = kind === 'invalid-json' ? { ok: true, json: () => Promise.reject(new Error('bad json')) }
      : response(kind === 'wrong-symbol' ? chartData({ symbol: '000660' }) : chartData({ bars: [{ ...chartData().bars[0], high: '1' }] }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(chartData())).mockResolvedValueOnce(badResponse));
    render(<View/>);
    await query();
    await query();
    expect(element('stock-chart').dataset.barCount).toBe('2');
    expect(screen.getByText('이전 조회 데이터')).toBeTruthy();
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('validates the code locally and aborts outstanding work on unmount', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<View/>);
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '123' } });
    await query();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByLabelText('차트 종목코드').getAttribute('aria-invalid')).toBe('true');
    fireEvent.change(screen.getByLabelText('차트 종목코드'), { target: { value: '005930' } });
    fireEvent.click(screen.getByRole('button', { name: '종목 차트 조회' }));
    view.unmount();
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(response(chartData())));
  });

  it('queries and focuses a quote selection independently of account and market refresh', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(response(url.startsWith('/api/chart?') ? chartData()
        : url === '/api/accounts' ? catalog : url === '/api/market' ? marketData({ symbols: ['005930'], quotes: [{ ...quote, name: '삼성전자' }] })
          : url.startsWith('/api/trades?') ? tradeHistory(url) : snapshot()));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<App/>);
    const quoteButton = await screen.findByRole('button', { name: '삼성전자 차트 보기' });
    expect(fetchMock.mock.calls.some(([url]) => String(url).startsWith('/api/chart'))).toBe(false);
    const count = fetchMock.mock.calls.length;
    await userEvent.click(quoteButton);
    await waitFor(() => expect(within(element('stock-chart')).getByRole('slider')).toBeTruthy());
    expect(fetchMock.mock.calls.slice(count).map(([url]) => url)).toEqual(['/api/chart?symbol=005930&interval=day']);
    expect(document.activeElement).toBe(element('stock-chart-title'));
    expect(element('stock-chart').scrollIntoView).toHaveBeenCalled();
  });
});
