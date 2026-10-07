import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ExperimentLabPanel } from '../components/ExperimentLab';
import { readExperiments } from '../experiments';
import useExperiments from '../useExperiments';
import { catalog, deferred, response } from './fixtures';
import { autonomy, configuredPolicy, experimentOrder, experimentRun, experiments } from './experiment-fixtures';

const props = { loading: false, pending: null, error: null, accounts: catalog.accounts, onRefresh: () => {},
  onExecute: async () => true, onSelectSymbol: () => {} };
function visibility(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
  fireEvent(document, new Event('visibilitychange'));
}
async function select(label: string, option: string) {
  await userEvent.click(screen.getByRole('combobox', { name: label }));
  await userEvent.click(screen.getByRole('option', { name: option }));
}

describe('experiment lab', () => {
  it('shows empty policy fields and actual provider availability without permitting an unconfigured start', () => {
    render(<ExperimentLabPanel {...props} data={experiments()}/>);
    expect(screen.getByText('Codex 미설정')).toBeTruthy();
    expect(screen.getByText('자동 주문 꺼짐')).toBeTruthy();
    expect(screen.getByRole('button', { name: '모의 자동 주문 시작' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: '설정 저장', hidden: true }).hasAttribute('disabled')).toBe(true);
    expect((screen.getByText('고급 운용 설정').closest('details') as HTMLDetailsElement).open).toBe(false);
    for (const label of ['실험 예산', '건당 매수 한도', '일일 매수 한도']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
    expect(screen.getByText('분석 기록 없음')).toBeTruthy();
    expect(screen.getByText('주문 기록 없음')).toBeTruthy();
    expect(screen.getAllByRole('article').map(card => card.querySelector('dd')?.textContent)).toEqual(['—원', '—원', '—원']);
  });

  it('saves explicit policy values without starting or placing an order', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    render(<ExperimentLabPanel {...props} onExecute={onExecute} data={experiments()}/>);
    await userEvent.click(screen.getByText('고급 운용 설정'));
    await select('모의계좌', catalog.accounts[0].name);
    await select('주문 실행 전략', '추세 규칙');
    await userEvent.type(screen.getByLabelText('실험 예산'), '3000000');
    await userEvent.type(screen.getByLabelText('건당 매수 한도'), '500000');
    await userEvent.type(screen.getByLabelText('일일 매수 한도'), '1000000');
    await userEvent.click(screen.getByRole('button', { name: '설정 저장' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'configure', policy: { ...configuredPolicy, account_id: catalog.accounts[0].id } });
    expect(screen.getByRole('button', { name: '모의 자동 주문 시작' }).hasAttribute('disabled')).toBe(true);
  });

  it('requires a saved policy to start and blocks edits while active, while pause remains available during background analysis', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    const data = experiments({ policy: configuredPolicy });
    const { rerender } = render(<ExperimentLabPanel {...props} onExecute={onExecute} data={data}/>);
    await userEvent.click(screen.getByRole('button', { name: '모의 자동 주문 시작' }));
    expect(onExecute).toHaveBeenLastCalledWith({ action: 'start' });
    rerender(<ExperimentLabPanel {...props} onExecute={onExecute} data={{ ...data, busy: true, automation: { enabled: true, state: 'running', pause_reason: null } }}/>);
    expect(screen.getByText('자동 주문 켜짐')).toBeTruthy();
    expect(screen.getByRole('button', { name: '분석 중' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: '신규 주문 정지' }));
    expect(onExecute).toHaveBeenLastCalledWith({ action: 'pause' });
    expect((screen.getByLabelText('실험 예산') as HTMLInputElement).disabled).toBe(true);
  });

  it('shows reasons, source IDs and unmodified scores, filters strategies and opens a chart', async () => {
    const onSelectSymbol = vi.fn();
    const signal = experimentRun().signals[0];
    render(<ExperimentLabPanel {...props} onSelectSymbol={onSelectSymbol} data={experiments({ runs: [experimentRun({ signals: [signal,
      { ...signal, strategy_id: 'llm', action: 'avoid', score: null, reason: 'Codex 결과 없음', evidence_ids: [] }] })] })}/>);
    expect(screen.getByText('75')).toBeTruthy();
    expect(screen.getByText('점수 없음')).toBeTruthy();
    await select('분석 전략', '추세 규칙');
    expect(screen.queryByText('점수 없음')).toBeNull();
    await userEvent.click(screen.getByText('판단 근거'));
    expect(screen.getByText('거래대금과 추세 조건 충족')).toBeTruthy();
    expect(screen.getByText('daily:005930:2026-10-02')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '삼성전자 일봉 차트 보기' }));
    expect(onSelectSymbol).toHaveBeenCalledWith('005930');
  });

  it('paginates analysis and order records independently', async () => {
    const signal = experimentRun().signals[0];
    render(<ExperimentLabPanel {...props} data={experiments({ runs: [experimentRun({ signals: Array.from({ length: 11 }, (_, index) => ({ ...signal, symbol: String(index).padStart(6, '0'), name: `종목${index}` })) })],
      orders: Array.from({ length: 11 }, (_, index) => experimentOrder({ id: `order-${index}`, symbol: String(index).padStart(6, '0'), name: `주문${index}` })) })}/>);
    await userEvent.click(within(screen.getByRole('navigation', { name: '분석 기록 페이지' })).getByRole('button', { name: '다음' }));
    expect(screen.getAllByRole('button', { name: /일봉 차트 보기/ })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /주문 종목 차트 보기/ })).toHaveLength(10);
    await userEvent.click(within(screen.getByRole('navigation', { name: '주문 기록 페이지' })).getByRole('button', { name: '다음' }));
    expect(screen.getAllByRole('button', { name: /주문 종목 차트 보기/ })).toHaveLength(1);
  });

  it('shows actual partial fills and cancels only accepted unfilled system orders with the local ID', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    render(<ExperimentLabPanel {...props} onExecute={onExecute} data={experiments({ orders: [experimentOrder(),
      experimentOrder({ id: 'done', name: '완료 종목', status: 'filled', filled_quantity: 3 }),
      experimentOrder({ id: 'unknown', name: '미확정 종목', status: 'unknown', order_id: null })] })}/>);
    expect(screen.getAllByRole('button', { name: /미체결 취소/ })).toHaveLength(1);
    expect(screen.getByText('부분 체결')).toBeTruthy();
    expect(screen.getAllByText('1 / 3주')).toHaveLength(2);
    await userEvent.click(screen.getByRole('button', { name: '삼성전자 미체결 취소' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'cancel', order_id: 'local-1' });
  });

  it('resolves uncertain submissions using explicit broker and branch IDs without discard or retry', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    render(<ExperimentLabPanel {...props} onExecute={onExecute} data={experiments({ orders: [experimentOrder({ id: 'uncertain', status: 'unknown', order_id: null })] })}/>);
    await userEvent.click(screen.getByText('접수 결과 대조'));
    await userEvent.type(screen.getByLabelText('삼성전자 증권사 주문번호'), '000123');
    await userEvent.type(screen.getByLabelText('삼성전자 주문 지점번호'), '001');
    await userEvent.click(screen.getByRole('button', { name: '주문 대조' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'resolve', id: 'uncertain', broker_order_id: '000123', branch_id: '001' });
    expect(screen.queryByRole('button', { name: /재주문|실패 처리/ })).toBeNull();
  });

  it('preserves results with visible errors and disables start until status is verified', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ policy: configuredPolicy, runs: [experimentRun()],
      llm: { configured: true, provider: 'codex', model: null, error: '로그인 확인 필요' } })} error="상태 조회 실패"/>);
    expect(screen.getByText('Codex 확인 필요')).toBeTruthy();
    expect(screen.getAllByRole('alert')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '삼성전자 일봉 차트 보기' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '모의 자동 주문 시작' }).hasAttribute('disabled')).toBe(true);
  });

  it('distinguishes missing shadow metrics, zero counts and signed event-mean returns from realized amounts', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ metrics: [
      { strategy_id: 'rules', closed_trades: 0, realized_pnl: '0', open_positions: 0, shadow_closed: 0, shadow_open: 2,
        shadow_pending: 4, shadow_version_count: 2, shadow_unknown: 1, shadow_excluded: 3, shadow_net_pct: null, shadow_stress_pct: null },
      { strategy_id: 'llm', closed_trades: 2, realized_pnl: '-4500', open_positions: 1, shadow_closed: 12, shadow_open: 0,
        shadow_unknown: 0, shadow_excluded: 0, shadow_net_pct: '1.257', shadow_stress_pct: '-0.154' },
    ] })}/>);
    const cards = screen.getAllByRole('article');
    expect(within(cards[0]).getByText('관찰 2')).toBeTruthy();
    expect(within(cards[0]).getByText('미확정 1')).toBeTruthy();
    expect(within(cards[0]).getByText('제외 3')).toBeTruthy();
    expect(within(cards[0]).getByText('진입 대기 4')).toBeTruthy();
    expect(within(cards[0]).getByText('버전 2개 합산')).toBeTruthy();
    expect(cards[0].querySelector('.lab-shadow-metrics dd')?.textContent).toBe('0');
    expect(cards[2].querySelector('.lab-shadow-metrics dd')?.textContent).toBe('—');
    expect(within(cards[1]).getByText('+1.26%')).toBeTruthy();
    expect(within(cards[1]).getByText('-0.15%')).toBeTruthy();
    expect(cards[1].querySelector('.lab-live-metrics dd')?.textContent).toBe('-4,500원');
    expect(screen.getAllByText('비용 추정 실현손익')).toHaveLength(3);
    expect(screen.getByText('가상 평균 · 신호일 동일가중 · 비용 후')).toBeTruthy();
  });

  it('shows autonomous account performance and timestamps separately from simulated strategy returns', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ autonomy: autonomy(), runs: [experimentRun()], orders: [experimentOrder()],
      policy: { ...configuredPolicy, execution_strategy: 'all-strategies-v1' }, automation: { enabled: true, state: 'running', pause_reason: null } })}/>);
    const performance = screen.getByLabelText('모의계좌 성과');
    expect(within(performance).getByText('10,125,000')).toBeTruthy();
    expect(within(performance).getByText('+1.25%')).toBeTruthy();
    expect(within(performance).getByText('-0.50%')).toBeTruthy();
    expect(screen.getByText('정상 운용')).toBeTruthy();
    expect(document.querySelector('.lab-status-line')?.textContent).toContain('전체 전략 균등');
    expect(screen.getByText('확인할 사항 없음')).toBeTruthy();
    expect(screen.getByLabelText('최근 자동 운용 확인').textContent).toContain('최근 분석2026. 10. 05. 09:00:00 KST');
    expect(screen.getByLabelText('최근 자동 운용 확인').textContent).toContain('최근 주문2026. 10. 05. 09:01:00 KST');
    expect(screen.getByText('주문 5 · 체결 3 · 확인 0')).toBeTruthy();
    expect((screen.getByText('고급 운용 설정').closest('details') as HTMLDetailsElement).open).toBe(false);
  });

  it('allows autonomous start with no manual monetary values and records only explicit answers to open issues', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    const issue = { id: 'issue-1', code: 'connection', message: '계좌 연결 확인 필요', question: '다시 연결할까요?', blocking: true,
      state: 'open' as const, first_seen: '2026-10-05T00:00:00Z', last_seen: '2026-10-05T01:00:00Z' };
    render(<ExperimentLabPanel {...props} onExecute={onExecute} data={experiments({ autonomy: autonomy({ status: 'attention',
      issues: [issue, { ...issue, id: 'resolved', message: '이미 해결된 문제', state: 'resolved' }], next_retry_at: '2026-10-05T01:05:00Z' }) })}/>);
    expect(screen.queryByText('이미 해결된 문제')).toBeNull();
    expect(screen.getByRole('button', { name: '모의 자동 주문 시작' }).hasAttribute('disabled')).toBe(false);
    expect(screen.getByText('다음 재시도')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '다시 시도' }));
    expect(onExecute).toHaveBeenLastCalledWith({ action: 'answer', id: 'issue-1', answer: 'retry' });
    await userEvent.click(screen.getByRole('button', { name: '중지 유지' }));
    expect(onExecute).toHaveBeenLastCalledWith({ action: 'answer', id: 'issue-1', answer: 'keep_paused' });
    expect(onExecute).toHaveBeenCalledTimes(2);
  });

  it('keeps unavailable autonomous performance blank and distinguishes automatic retry from paused state', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ autonomy: autonomy({ status: 'degraded', error: '계좌 응답 지연',
      performance: { as_of: null, baseline: null, total_value: null, cash: null, return_pct: null, max_drawdown_pct: null, observations: 0 }, daily_reports: [] }) })}/>);
    expect(screen.getByText('자동 복구 중')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toBe('계좌 응답 지연');
    expect(screen.getByLabelText('모의계좌 성과').querySelectorAll('dd')[1].textContent).toBe('—');
  });
});

describe('experiment requests', () => {
  it('polls GET every 20 seconds, preserves data on failures and recovers', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments({ runs: [experimentRun()] })))
      .mockResolvedValueOnce(response({}, 503)).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    await act(async () => {});
    expect(fetchMock.mock.calls[0]).toEqual(['/api/experiments', expect.objectContaining({ method: 'GET', cache: 'no-store', headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' } })]);
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(result.current.error).toBe('실험실을 불러오지 못했습니다.');
    expect(result.current.data?.runs).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(result.current.error).toBeNull();
    expect(result.current.data?.runs).toHaveLength(0);
  });

  it('makes one protected POST per action and never retries a failed mutation', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments())).mockRejectedValueOnce(new Error('connection lost')).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    await act(async () => {});
    await act(async () => { await result.current.execute({ action: 'start' }); });
    expect(fetchMock.mock.calls[1]).toEqual(['/api/experiments', expect.objectContaining({ method: 'POST', body: '{"action":"start"}',
      headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1', 'Content-Type': 'application/json' } })]);
    expect(result.current.error).toContain('요청 결과를 확인하지 못했습니다');
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  });

  it('keeps backend validation errors and does not change saved state', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments())).mockResolvedValue(response({ error: '모의계좌 선택 필요' }, 400));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    await act(async () => {});
    await act(async () => { expect(await result.current.execute({ action: 'start' })).toBe(false); });
    expect(result.current.error).toBe('모의계좌 선택 필요');
    expect(result.current.data?.automation.enabled).toBe(false);
  });

  it('accepts asynchronous action snapshots and blocks overlapping commands', async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments())).mockReturnValueOnce(pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    await act(async () => {});
    act(() => { void result.current.execute({ action: 'analyze' }); });
    await act(async () => { expect(await result.current.execute({ action: 'start' })).toBe(false); });
    expect(result.current.pending).toBe('analyze');
    await act(async () => pending.resolve(response(experiments({ busy: true, status: 'running' }), 202)));
    expect(result.current.pending).toBeNull();
    expect(result.current.data?.busy).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('aborts hidden GET requests, ignores stale responses and resumes only GET when visible', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    act(() => visibility(true));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => visibility(false));
    await act(async () => pending.resolve(response(experiments({ runs: [experimentRun()] }))));
    expect(result.current.data?.runs).toHaveLength(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not abort or duplicate a submitted command when the tab becomes hidden', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments())).mockReturnValueOnce(pending.promise).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments());
    await act(async () => {});
    act(() => { void result.current.execute({ action: 'pause' }); });
    act(() => visibility(true));
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(false);
    await act(async () => pending.resolve(response(experiments())));
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => visibility(false));
    expect(fetchMock.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  });

  it('times out a mutation, ignores its late response and cleans up all polling on unmount', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(experiments())).mockReturnValueOnce(pending.promise).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result, unmount } = renderHook(() => useExperiments());
    await act(async () => {});
    act(() => { void result.current.execute({ action: 'start' }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
    expect(result.current.error).toContain('요청 결과를 확인하지 못했습니다');
    expect(result.current.pending).toBeNull();
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(response(experiments({ automation: { enabled: true, state: 'running', pause_reason: null } }))));
    expect(result.current.data?.automation.enabled).toBe(false);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('survives StrictMode remounts without accepting superseded fetches', async () => {
    vi.useFakeTimers();
    const pending = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue(response(experiments()));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useExperiments(), { wrapper: StrictMode });
    await act(async () => {});
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => pending.resolve(response(experiments({ runs: [experimentRun()] }))));
    expect(result.current.data?.runs).toHaveLength(0);
    const count = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(fetchMock).toHaveBeenCalledTimes(count + 1);
  });
});

describe('experiment response validation', () => {
  it('accepts the paper response and extra backend fields', () => {
    const data = { ...experiments({ runs: [experimentRun()], orders: [experimentOrder()], autonomy: autonomy() }), additional: 'allowed' };
    expect(readExperiments(data)).toBe(data);
  });
  it.each([
    { ...experiments(), environment: 'real' },
    { ...experiments(), llm: null },
    { ...experiments(), updated_at: 'invalid' },
    { ...experiments(), runs: [experimentRun({ signals: [{ ...experimentRun().signals[0], score: NaN }] })] },
    { ...experiments(), orders: [experimentOrder({ filled_quantity: 4 })] },
    { ...experiments(), orders: [experimentOrder({ average_price: 'NaN' })] },
    { ...experiments(), positions: [{ strategy_id: 'rules', symbol: 'bad', name: '', quantity: 1, average_price: '100' }] },
    { ...experiments(), metrics: [{ strategy_id: 'rules', closed_trades: 0, realized_pnl: '0', open_positions: 0, shadow_closed: -1 }] },
    { ...experiments(), metrics: [{ strategy_id: 'rules', closed_trades: 0, realized_pnl: '0', open_positions: 0, shadow_net_pct: 'NaN' }] },
    { ...experiments(), autonomy: { ...autonomy(), status: 'unrecognized' } },
    { ...experiments(), autonomy: { ...autonomy(), performance: { ...autonomy().performance, observations: -1 } } },
    { ...experiments(), autonomy: { ...autonomy(), daily_reports: [{ ...autonomy().daily_reports[0], orders: '5' }] } },
  ])('rejects malformed or non-paper responses', value => { expect(() => readExperiments(value)).toThrow(); });
});
