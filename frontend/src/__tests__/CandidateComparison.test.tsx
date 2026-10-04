import { StrictMode } from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import CandidateComparison from '../components/CandidateComparison';
import type { CandidateComparisonData, CandidateRow } from '../candidates';
import useCandidateComparison from '../useCandidateComparison';
import { catalog, deferred, element, marketData, response, snapshot, tradeHistory } from './fixtures';

function row(overrides: Partial<CandidateRow> = {}): CandidateRow {
  return { symbol: '005930', name: '삼성전자', board: 'KOSPI', status: 'ok', error: null, as_of: '2026-10-02',
    close: '61200', return_5d_pct: '2', return_20d_pct: '15', excess_5d_pp: '1.25', excess_20d_pp: '10',
    avg_turnover_20d: '12345000000', turnover_ratio: '1.5', ...overrides };
}

function comparison(overrides: Partial<CandidateComparisonData> = {}): CandidateComparisonData {
  return { status: 'complete', error: null, updated_at: '2026-10-03T04:00:00Z', as_of: '2026-10-02', stale: false,
    progress: { completed: 1, total: 1 }, universe: { status: 'verified', as_of: '2026-10-02', checked_at: '2026-10-03T04:00:00Z',
      source_url: 'https://www.truefriend.com/league', count: 1, error: null }, rows: [row()], ...overrides };
}

function screenedComparison(): CandidateComparisonData {
  const rows = [
    row({ symbol: '000001', name: '첫 후보', excess_20d_pp: '1', selection: { status: 'selected', rank: 1, score: '12', reasons: [] } }),
    row({ symbol: '000002', name: '둘째 후보', board: 'KOSDAQ', excess_20d_pp: '99', selection: { status: 'selected', rank: 2, score: '8', reasons: [] } }),
    row({ symbol: '000003', name: '대기 종목', selection: { status: 'reserve', rank: 3, score: '4', reasons: ['순위 범위 밖'] } }),
    row({ symbol: '000004', name: '제외 종목', selection: { status: 'excluded', rank: null, score: null, reasons: ['시장경보'] } }),
    row({ symbol: '000005', name: '미확인 종목', selection: { status: 'unverified', rank: null, score: null, reasons: ['상태 조회 실패'] } }),
  ];
  return comparison({ rows, universe: { ...comparison().universe, count: 5 }, screening: {
    policy_id: 'test-policy', status: 'ready', label: '관찰 기준', score_label: '선별 상대강도', score_unit: '%p',
    criteria: ['일평균 거래대금 100억원 이상', '거래제한 상태 확인'], checked_at: '2026-10-03T04:00:00Z',
    master_observed_at: '2026-10-03T03:00:00Z', counts: { selected: 2, reserve: 1, excluded: 1, unverified: 1 }, error: null,
  } });
}

function Harness() {
  const candidate = useCandidateComparison();
  return <CandidateComparison data={candidate.data} loading={candidate.loading} error={candidate.error}
    onStart={() => { void candidate.start(); }} onRefresh={() => { void candidate.refresh(); }} onSelectSymbol={() => {}}/>;
}

function display(data = comparison(), select = vi.fn()) {
  return render(<CandidateComparison data={data} loading={false} error={null} onStart={vi.fn()} onRefresh={vi.fn()} onSelectSymbol={select}/>);
}

function displayedSymbols() { return [...element('candidate-comparison').querySelectorAll('tbody tr')].map(tr => tr.getAttribute('data-symbol')); }

async function chooseFilter(label: string, option: string) {
  await userEvent.click(screen.getByRole('combobox', { name: label }));
  await userEvent.click(screen.getByRole('option', { name: option }));
}

describe('candidate comparison', () => {
  it('defaults to selected candidates in screening order and exposes exclusions separately', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(screenedComparison())));
    render(<Harness/>);
    await screen.findByRole('button', { name: '첫 후보 차트 보기' });
    expect(displayedSymbols()).toEqual(['000001', '000002']);
    expect(screen.getByLabelText('후보 선별 집계').textContent).toContain('관찰 후보 2');
    await userEvent.click(screen.getByText('선별 기준', { exact: true }));
    expect(screen.getByText('일평균 거래대금 100억원 이상')).toBeTruthy();
    await chooseFilter('선별 상태', '제외');
    expect(displayedSymbols()).toEqual(['000004']);
    expect(screen.getByText('제외 · 시장경보')).toBeTruthy();
    await chooseFilter('선별 상태', '확인 필요');
    expect(displayedSymbols()).toEqual(['000005']);
    expect(screen.getByText('확인 필요 · 상태 조회 실패')).toBeTruthy();
    expect(screen.getByLabelText('후보 선별 집계').textContent).toContain('관찰 후보 2');
  });

  it('shows no candidates without silently substituting excluded or unknown stocks', () => {
    const data = screenedComparison();
    data.rows = data.rows.filter(value => value.selection?.status !== 'selected');
    data.screening!.counts.selected = 0;
    display(data);
    expect(displayedSymbols()).toEqual([]);
    expect(screen.getByText('확인된 관찰 후보 없음 · 미확정 1종목')).toBeTruthy();
  });

  it('distinguishes a confirmed no-signal result from an incomplete screening', () => {
    const data = screenedComparison();
    data.rows = data.rows.filter(value => value.selection?.status === 'excluded');
    data.screening!.counts = { selected: 0, reserve: 0, excluded: 1, unverified: 0 };
    display(data);
    expect(screen.getByText('현재 조건을 통과한 관찰 후보 없음')).toBeTruthy();
  });

  it('preserves server ranking when precise scores collapse to the same browser number', () => {
    const data = screenedComparison();
    data.rows[0].selection = { status: 'selected', rank: 2, score: '12.000000000000000001', reasons: [] };
    data.rows[1].selection = { status: 'selected', rank: 1, score: '12.000000000000000002', reasons: [] };
    display(data);
    expect(displayedSymbols()).toEqual(['000002', '000001']);
  });

  it.each([['20일 평균 거래대금', '억원', '12억'], ['20일 지수 대비', '%p', '+12%p']])(
    'shows the ranking metric only once and preserves its mobile unit: %s', (label, unit, rendered) => {
      const data = screenedComparison();
      data.screening!.score_label = label;
      data.screening!.score_unit = unit;
      display(data);
      const firstRow = element('candidate-comparison').querySelector('tbody tr')!;
      expect(firstRow.querySelectorAll('.candidate-metric')).toHaveLength(7);
      expect([...firstRow.querySelectorAll('.candidate-mobile-label')].filter(node => node.textContent === label)).toHaveLength(1);
      expect(firstRow.textContent).toContain(rendered);
    });

  it('preserves the last screening result and its counts while an empty refresh is running', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(screenedComparison()))
      .mockResolvedValue(response(comparison({ status: 'running', rows: [], as_of: null, updated_at: null })));
    vi.stubGlobal('fetch', fetchMock);
    render(<Harness/>);
    await screen.findByRole('button', { name: '첫 후보 차트 보기' });
    await userEvent.click(screen.getByRole('button', { name: '전체 조회' }));
    expect(displayedSymbols()).toEqual(['000001', '000002']);
    expect(screen.getByLabelText('후보 선별 집계').textContent).toContain('관찰 후보 2');
    expect(screen.getByText(/비교 중 · 이전 결과/)).toBeTruthy();
  });

  it.each(['counts', 'missing-selection', 'bad-score'] as const)('rejects inconsistent screening metadata: %s', async kind => {
    const bad = screenedComparison();
    if (kind === 'counts') bad.screening!.counts.selected = 20;
    if (kind === 'missing-selection') delete bad.rows[0].selection;
    if (kind === 'bad-score') bad.rows[0].selection!.score = 'NaN';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(bad)));
    render(<Harness/>);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(displayedSymbols()).toEqual([]);
  });

  it('formats every metric, source and dates and keeps the selected stock connected', async () => {
    const select = vi.fn();
    display(comparison(), select);
    expect(screen.getByText('61,200')).toBeTruthy();
    expect(screen.getByText('+2.00%').classList.contains('gain') || screen.getByText('+2.00%').parentElement?.classList.contains('gain')).toBe(true);
    expect(screen.getByText('+15.00%')).toBeTruthy();
    expect(screen.getByText('+1.25%p')).toBeTruthy();
    expect(screen.getByText('+10%p')).toBeTruthy();
    expect(screen.getByText('123.45억')).toBeTruthy();
    expect(screen.getByText('1.5배')).toBeTruthy();
    expect(screen.getByRole('link', { name: '대상 목록 출처' }).getAttribute('href')).toBe('https://www.truefriend.com/league');
    expect(screen.getByRole('columnheader', { name: /수정종가/ })).toBeTruthy();
    expect(screen.getByText('대회 대상 종목 전체')).toBeTruthy();
    expect(screen.getByText(/13:00:00 KST/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '삼성전자 차트 보기' }));
    expect(select).toHaveBeenCalledWith('005930');
  });

  it('sorts numbers instead of strings and leaves missing values last in either direction', async () => {
    display(comparison({ rows: [row({ symbol: '000001', excess_20d_pp: '2' }), row({ symbol: '000002', excess_20d_pp: '100' }),
      row({ symbol: '000003', status: 'excluded', error: '20거래일 미충족', excess_20d_pp: null })] }));
    expect(displayedSymbols()).toEqual(['000002', '000001', '000003']);
    await userEvent.click(screen.getByRole('button', { name: '정렬 방향: 내림차순' }));
    expect(displayedSymbols()).toEqual(['000001', '000002', '000003']);
    expect(screen.getByRole('columnheader', { name: /20일 지수 대비/ }).getAttribute('aria-sort')).toBe('ascending');
  });

  it('accepts an alphanumeric candidate symbol and matches a lowercase search', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(comparison({ rows: [row({ symbol: '0126Z0', name: '삼성에피스홀딩스' })] }))));
    render(<Harness/>);
    await screen.findByRole('button', { name: '삼성에피스홀딩스 차트 보기' });
    await userEvent.type(screen.getByLabelText('종목 검색'), '0126z0');
    expect(displayedSymbols()).toEqual(['0126Z0']);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('filters and paginates 25 rows without changing overall status counts', async () => {
    const rows = Array.from({ length: 31 }, (_, index) => row({ symbol: String(index).padStart(6, '0'), name: `테스트 ${index}`,
      board: index === 30 ? 'KOSDAQ' : 'KOSPI', excess_20d_pp: String(31 - index),
      status: index === 0 ? 'error' : index === 1 ? 'excluded' : 'ok', error: index === 0 ? '조회 실패' : null }));
    display(comparison({ universe: { ...comparison().universe, count: 31 }, rows }));
    expect(displayedSymbols()).toHaveLength(25);
    await userEvent.click(screen.getByRole('button', { name: '다음' }));
    expect(displayedSymbols()).toHaveLength(6);
    expect(screen.getByText('31종목 · 2 / 2페이지')).toBeTruthy();
    await chooseFilter('시장', 'KOSDAQ');
    expect(displayedSymbols()).toEqual(['000030']);
    expect(screen.getByText('1종목 · 1 / 1페이지')).toBeTruthy();
    expect(screen.getByText('전체 31')).toBeTruthy();
    expect(screen.getByText('성공 29')).toBeTruthy();
    expect(screen.getByText('제외 1')).toBeTruthy();
    expect(screen.getByText('실패 1')).toBeTruthy();
    await userEvent.type(screen.getByLabelText('종목 검색'), '없는종목');
    expect(screen.getByText('검색 결과 없음')).toBeTruthy();
    await userEvent.clear(screen.getByLabelText('종목 검색'));
    await userEvent.type(screen.getByLabelText('종목 검색'), '000030');
    expect(displayedSymbols()).toEqual(['000030']);
  });

  it('disables full collection until the universe is verified and rejects unsafe source URLs', () => {
    display(comparison({ status: 'idle', rows: [], universe: { ...comparison().universe, status: 'unverified',
      source_url: 'javascript:alert(1)', error: '대회 대상 목록 확인 필요' } }));
    expect((screen.getByRole('button', { name: '전체 조회' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toBe('대회 대상 목록 확인 필요');
    expect(screen.queryByRole('link', { name: '대상 목록 출처' })).toBeNull();
  });

  it('loads saved state without starting collection and posts once when explicitly requested', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(comparison())).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    render(<Harness/>);
    await screen.findByText('61,200');
    expect(fetchMock.mock.calls).toHaveLength(1);
    expect(fetchMock.mock.calls[0][1].method).toBe('GET');
    await userEvent.dblClick(screen.getByRole('button', { name: '전체 조회' }));
    expect(fetchMock.mock.calls).toHaveLength(2);
    expect(fetchMock.mock.calls[1][0]).toBe('/api/candidates');
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'POST', body: '{}',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-KIS-Dashboard': '1' } });
    await act(async () => pending.resolve(response(comparison({ status: 'running', rows: [], as_of: null, updated_at: null,
      progress: { completed: 0, total: 100 } }))));
    expect(screen.getByText('61,200')).toBeTruthy();
    expect(screen.getByText('비교 중 · 이전 결과 · 0 / 100')).toBeTruthy();
    expect(screen.getByText(/13:00:00 KST/)).toBeTruthy();
    expect(element('candidate-comparison').querySelector('.candidate-meta time')?.getAttribute('datetime')).toBe('2026-10-02');
  });

  it('polls every two seconds while running, retries a polling failure and stops at completion', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(comparison({ status: 'running' })))
      .mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(comparison()));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<Harness/>);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('alert').textContent).toContain('불러오지 못했습니다');
    expect(screen.getByText('61,200')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(screen.getByText('비교 완료')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(8_000); });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains previous rows and marks stale after an HTTP error and recovers by refreshing state', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(comparison()))
      .mockResolvedValueOnce(response(comparison({ status: 'error', rows: [], as_of: null, updated_at: null, error: '일봉 조회 실패' }), 503))
      .mockResolvedValueOnce(response(comparison({ rows: [row({ close: '62000' })] })));
    vi.stubGlobal('fetch', fetchMock);
    render(<Harness/>);
    await screen.findByText('61,200');
    await userEvent.click(screen.getByRole('button', { name: '전체 조회' }));
    expect(await screen.findByText('일봉 조회 실패')).toBeTruthy();
    expect(screen.getByText('61,200')).toBeTruthy();
    expect(screen.getByText('상태 확인 필요 · 이전 결과')).toBeTruthy();
    expect(screen.getByText(/13:00:00 KST/)).toBeTruthy();
    expect(element('candidate-comparison').querySelector('.candidate-meta time')?.getAttribute('datetime')).toBe('2026-10-02');
    await userEvent.click(screen.getByRole('button', { name: '상태 다시 확인' }));
    expect(await screen.findByText('62,000')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each([false, true])('distinguishes completed collection with row failures (partial: %s)', partial => {
    const failed = row({ symbol: '000660', status: 'error', error: '일봉 응답 실패', close: null });
    display(comparison({ status: 'complete', error: '실패 종목 확인 필요', rows: partial ? [row(), failed] : [failed] }));
    expect(screen.getByText(partial ? '일부 조회 실패' : '조회 실패')).toBeTruthy();
    expect(screen.queryByText('비교 완료')).toBeNull();
  });

  it.each(['duplicate', 'nonfinite', 'invalid-date'] as const)('rejects malformed candidate data: %s', async kind => {
    const bad = kind === 'duplicate' ? comparison({ rows: [row(), row()] })
      : kind === 'nonfinite' ? comparison({ rows: [row({ return_5d_pct: 'Infinity' })] })
        : comparison({ as_of: '2026-02-30' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(bad)));
    render(<Harness/>);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(element('candidate-comparison').dataset.rowCount).toBe('0');
  });

  it('ignores an aborted StrictMode response, uses one polling timer and cleans up on unmount', async () => {
    vi.useFakeTimers();
    const first = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(response(comparison({ status: 'running' })));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<StrictMode><Harness/></StrictMode>);
    await act(async () => {});
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => first.resolve(response(comparison({ rows: [row({ close: '999' })] }))));
    expect(screen.queryByText('999')).toBeNull();
    const initial = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initial + 1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(fetchMock).toHaveBeenCalledTimes(initial + 1);
  });

  it.each(['005930', '0126Z0'])('starts the existing stock chart for %s without starting collection', async symbol => {
    window.history.replaceState(null, '', '/#/candidates');
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(response(url === '/api/candidates' ? comparison({ rows: [row({ symbol })] }) : url === '/api/accounts' ? catalog
        : url === '/api/market' ? marketData() : url.startsWith('/api/trades?') ? tradeHistory(url)
          : url.startsWith('/api/chart?') ? { status: 'error', error: '차트 확인', symbol, interval: 'day', market: 'KRX',
            environment: 'paper', source: 'KIS', adjusted: true, updated_at: null, as_of: null, stale: false, bars: [] } : snapshot()));
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<App/>);
    const panel = within(element('candidate-comparison'));
    await userEvent.click(await panel.findByRole('button', { name: '삼성전자 차트 보기' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === `/api/chart?symbol=${symbol}&interval=day`)).toBe(true));
    expect(document.activeElement).toBe(element('page-title'));
    expect(window.location.hash).toBe(`#/chart?symbol=${symbol}&interval=day`);
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/candidates')).toHaveLength(1);
  });
});
