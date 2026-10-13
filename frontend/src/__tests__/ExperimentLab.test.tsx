import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ExperimentLabPanel } from '../components/ExperimentLab';
import { readExperiments } from '../experiments';
import useExperiments from '../useExperiments';
import { catalog, deferred, response } from './fixtures';
import { autonomy, configuredPolicy, dailyLearning, experimentOrder, experimentRun, experiments, learning, learningAccount } from './experiment-fixtures';

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
    for (const label of ['운용 예산', '건당 매수 한도', '일일 매수 한도']) expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
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
    await userEvent.type(screen.getByLabelText('운용 예산'), '3000000');
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
    expect((screen.getByLabelText('운용 예산') as HTMLInputElement).disabled).toBe(true);
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

  it('keeps learning absent for legacy data and shows only supplied candidate evaluation facts', () => {
    const data = experiments({ policy: { ...configuredPolicy, execution_strategy: 'all-strategies-v1' } });
    const { rerender } = render(<ExperimentLabPanel {...props} data={data}/>);
    expect(screen.queryByRole('region', { name: '학습·전략 교체' })).toBeNull();
    expect(document.querySelector('.lab-status-line')?.textContent).toContain('전체 전략 균등');
    rerender(<ExperimentLabPanel {...props} data={{ ...data, learning: learning() }}/>);
    const panel = screen.getByRole('region', { name: '학습·전략 교체' });
    expect(within(panel).getByText('후보 평가 중')).toBeTruthy();
    expect(within(panel).getByText('거래량 회복 후보')).toBeTruthy();
    expect(within(panel).getByText('4 / 20구간 · 완결 3건')).toBeTruthy();
    expect(panel.textContent).toContain('수익률 4구간');
    expect(within(panel).getByText('평가 기간 부족')).toBeTruthy();
    expect(within(panel).getByText('+1.75%')).toBeTruthy();
    expect(within(panel).getByText('-0.25%')).toBeTruthy();
    expect(document.querySelector('.lab-status-line')?.textContent).toContain('자동 전략 운용');
  });

  it('shows missing evaluation values as unavailable and keeps errors and last adoption visible', () => {
    const current = learning();
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ status: 'error',
      champion: { id: 'rules', name: '추세 규칙', adopted_at: '2026-10-08T06:40:00Z' },
      challenger: null, last_evaluation: { ...current.last_evaluation!, champion_return_pct: null,
        challenger_return_pct: 0, champion_drawdown_pct: null, challenger_drawdown_pct: 0 },
      last_change: { at: '2026-10-08T06:40:00Z', from: 'llm', to: 'rules', reason: '후보 평가 통과' },
      error: '후보 자료 조회 실패' }) })}/>);
    const panel = screen.getByRole('region', { name: '학습·전략 교체' });
    expect(within(panel).getByText('없음')).toBeTruthy();
    expect(within(panel).getAllByText('—')).toHaveLength(2);
    expect(within(panel).getAllByText('0.00%')).toHaveLength(2);
    expect(within(panel).getByText('LLM 판단 → 추세 규칙')).toBeTruthy();
    expect(within(panel).getByText('후보 평가 통과')).toBeTruthy();
    expect(within(panel).getByRole('alert').textContent).toBe('후보 자료 조회 실패');
  });

  it.each([['disabled', '중지'], ['waiting', '대기'], ['researching', '후보 연구 중']] as const)('shows learning status %s without inventing a candidate', (status, label) => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ enabled: status !== 'disabled', status,
      challenger: null, last_evaluation: null }) })}/>);
    const panel = screen.getByRole('region', { name: '학습·전략 교체' });
    expect(within(panel).getByText(label)).toBeTruthy();
    expect(within(panel).getByText('평가 기록 없음')).toBeTruthy();
    expect(within(panel).queryByRole('table')).toBeNull();
  });

  it.each([['invalid', '평가 무효'], ['rollback', '이전 전략 복귀']] as const)('labels backend evaluation decision %s', (decision, label) => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({
      last_evaluation: { ...learning().last_evaluation!, decision } }) })}/>);
    expect(within(screen.getByRole('region', { name: '학습·전략 교체' })).getByText(label)).toBeTruthy();
  });

  it('keeps learned strategies available for analysis filtering but not standalone execution', async () => {
    const data = experiments({ runs: [experimentRun()] });
    data.strategies.push({ id: 'learned-1', label: '학습 후보', description: '', version: 'policy-1', selectable: false });
    render(<ExperimentLabPanel {...props} data={data}/>);
    await userEvent.click(screen.getByText('고급 운용 설정'));
    await userEvent.click(screen.getByRole('combobox', { name: '주문 실행 전략' }));
    expect(screen.queryByRole('option', { name: '학습 후보' })).toBeNull();
    await userEvent.keyboard('{Escape}');
    await userEvent.click(screen.getByRole('combobox', { name: '분석 전략' }));
    expect(screen.getByRole('option', { name: '학습 후보' })).toBeTruthy();
  });

  it('separates AI owned performance and model comparisons from whole paper account performance', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ autonomy: autonomy(), learning: learning({ account: learningAccount(),
      last_evaluation: { ...learning().last_evaluation!, phase: 'paper_provisional' } }) })}/>);
    const owned = screen.getByRole('group', { name: 'AI 운용 성과' });
    expect(within(owned).getByText('1,025,000원')).toBeTruthy();
    expect(within(owned).getByText('+2.50%')).toBeTruthy();
    expect(within(owned).getByText('4.00%')).toBeTruthy();
    expect(within(owned).getByText('700,000원')).toBeTruthy();
    expect(within(owned).getByText('손실 보호 정상')).toBeTruthy();
    expect(within(screen.getByLabelText('모의계좌 성과')).getByText('10,125,000')).toBeTruthy();
    const comparison = within(owned).getByRole('table', { name: 'AI 운용과 비교 기준 수익률' });
    const rows = within(comparison).getAllByRole('row');
    expect(rows[1].textContent).toBe('최초 정책+1.50%-0.5%p');
    expect(rows[2].textContent).toBe('현금0.00%+1%p');
    expect(rows[3].textContent).toBe('KOSPI 가격지수+3.00%-2%p');
    expect(within(owned).getByText('비교 모형 ·', { exact: false }).textContent).toBe('비교 모형 · 2026-10-05부터 · 동기간 AI +1.00%');
    expect(screen.getByText('모의 비교')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '손실 보호 해제' })).toBeNull();
  });

  it.each(['waiting', 'invalid'])('never derives relative results from %s comparisons', status => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ account: learningAccount({
      comparisons: { ...learningAccount().comparisons!, status, error: '비교 관측 부족' } }) }) })}/>);
    const table = screen.getByRole('table', { name: 'AI 운용과 비교 기준 수익률' });
    for (const row of within(table).getAllByRole('row').slice(1)) expect(within(row).getAllByRole('cell')[1].textContent).toBe('—');
    expect(screen.getByRole('alert').textContent).toBe('비교 관측 부족');
  });

  it('keeps unavailable benchmark values blank without hiding available comparisons', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ account: learningAccount({
      comparisons: { ...learningAccount().comparisons!, market_return_pct: null, error: '시장 지수 자료 없음' } }) }) })}/>);
    const rows = within(screen.getByRole('table', { name: 'AI 운용과 비교 기준 수익률' })).getAllByRole('row');
    expect(rows[1].textContent).toBe('최초 정책+1.50%-0.5%p');
    expect(rows[3].textContent).toBe('KOSPI 가격지수——');
  });

  it('resets active protection once without enabling orders', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    const account = learningAccount({ risk: { active: true, limit_pct: 15, drawdown_pct: 16, triggered_at: '2026-10-09T06:30:00Z' } });
    const data = experiments({ policy: configuredPolicy, learning: learning({ account }) });
    const { rerender } = render(<ExperimentLabPanel {...props} onExecute={onExecute} data={data}/>);
    expect(screen.getByText('손실 보호 발동')).toBeTruthy();
    expect(screen.getByRole('button', { name: '보호 청산 재개' }).hasAttribute('disabled')).toBe(false);
    await userEvent.click(screen.getByRole('button', { name: '손실 보호 해제' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'reset_risk' });
    rerender(<ExperimentLabPanel {...props} onExecute={onExecute} data={{ ...data, learning: learning({ account: learningAccount() }) }}/>);
    expect(screen.getByText('자동 주문 꺼짐')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '손실 보호 해제' })).toBeNull();
    expect(onExecute).toHaveBeenCalledTimes(1);
  });

  it('resumes protective liquidation without resetting protection while owned positions remain', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    const data = experiments({ policy: configuredPolicy, learning: learning({ account: learningAccount({
      risk: { active: true, limit_pct: 15, drawdown_pct: 16 } }) }),
      positions: [{ strategy_id: 'rules', symbol: '005930', name: '삼성전자', quantity: 1, average_price: '100000' }] });
    render(<ExperimentLabPanel {...props} onExecute={onExecute} data={data}/>);
    expect(screen.getByRole('button', { name: '손실 보호 해제' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: '보호 청산 재개' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'start' });
    expect(screen.getByText('손실 보호 발동')).toBeTruthy();
  });

  it('leaves relative returns blank when same-period AI performance is unavailable', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ account: learningAccount({
      comparisons: { ...learningAccount().comparisons!, ai_return_pct: null } }) }) })}/>);
    const table = screen.getByRole('table', { name: 'AI 운용과 비교 기준 수익률' });
    for (const row of within(table).getAllByRole('row').slice(1)) expect(within(row).getAllByRole('cell')[1].textContent).toBe('—');
    expect(within(screen.getByRole('group', { name: 'AI 운용 성과' })).getByText('+2.50%')).toBeTruthy();
  });

  it.each(['enabled', 'position', 'unfilled', 'unknown', 'pending'] as const)('blocks protection reset while %s', reason => {
    const data = experiments({ learning: learning({ account: learningAccount({ risk: { active: true, limit_pct: 15, drawdown_pct: 16 } }) }) });
    if (reason === 'enabled') data.automation.enabled = true;
    if (reason === 'position') data.positions = [{ strategy_id: 'rules', symbol: '005930', name: '삼성전자', quantity: 1, average_price: '100000' }];
    if (reason === 'unfilled') data.orders = [experimentOrder()];
    if (reason === 'unknown') data.orders = [experimentOrder({ status: 'unknown', filled_quantity: 3 })];
    render(<ExperimentLabPanel {...props} pending={reason === 'pending' ? 'reset_risk' : null} data={data}/>);
    expect(screen.getByRole('button', { name: reason === 'pending' ? '보호 해제 중' : '손실 보호 해제' }).hasAttribute('disabled')).toBe(true);
  });
});

describe('daily learning display', () => {
  it('shows the active adoption comparison separately from development and validates returns', () => {
    const comparison = { decision_id: 'decision-1', effective_day: '2026-10-12', status: 'ready' as const,
      basis: 'post_activation_paired_model' as const, start_day: '2026-10-09', end_day: '2026-10-13', intervals: 2,
      unchanged: { net_return_pct: 1, stress_return_pct: .8 }, candidate: { net_return_pct: 1.2, stress_return_pct: .9 },
      delta_net_return_pp: .2, delta_stress_return_pp: .1, worst_delta_net_return_pp: -.1, best_delta_net_return_pp: .2 };
    const data = experiments({ learning: learning({ daily: dailyLearning({ forward_comparisons: [comparison] }) }) });
    expect(readExperiments(data)).toBe(data);
    const { rerender } = render(<ExperimentLabPanel {...props} data={data}/>);
    const section = screen.getByText('적용 후 비교 모형').closest('.lab-daily-decision')!;
    expect(section.textContent).toContain('2026-10-09 ~ 2026-10-13 · 2구간');
    expect(section.textContent).toContain('기존 유지+1.00%적용 정책+1.20%수익률 차이+0.20%p');
    expect(section.textContent).toContain('집행 시나리오 차이 -0.10%p ~ +0.20%p');
    const waiting = experiments({ learning: learning({ daily: dailyLearning({ forward_comparisons: [
      { decision_id: 'decision-1', effective_day: '2026-10-12', status: 'unavailable' }] }) }) });
    rerender(<ExperimentLabPanel {...props} data={waiting}/>);
    expect(screen.getByText('비교 자료 확인 필요')).toBeTruthy();
    expect(screen.queryByText('+0.20%p')).toBeNull();
    comparison.delta_net_return_pp = NaN;
    expect(() => readExperiments(data)).toThrow('일일 개선 응답 형식 확인 필요');
  });

  it('waits for completed signals before showing next-session application as prepared', () => {
    const daily = dailyLearning({ active: null, pending: { ...dailyLearning().pending!, prepared_at: null } });
    const { rerender } = render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily }) })}/>);
    let panel = screen.getByRole('region', { name: '일일 개선' });
    expect(within(panel).getByRole('status').textContent).toBe('신호 준비 대기');
    expect(panel.querySelector('.lab-daily-learning')?.textContent).toContain('현재 적용 정책추세 규칙');
    expect(within(panel).queryByText('다음 거래일 적용 대기')).toBeNull();
    rerender(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily: {
      ...daily, pending: { ...daily.pending!, prepared_at: '2026-10-12T07:15:00Z' },
    } }) })}/>);
    panel = screen.getByRole('region', { name: '일일 개선' });
    expect(within(panel).getByRole('status').textContent).toBe('다음 거래일 적용 대기');
    expect(within(panel).queryByText('신호 준비 대기')).toBeNull();
  });

  it('separates the active policy from the next policy and keeps long-term evaluation secondary', () => {
    render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily: dailyLearning(),
      last_evaluation: { ...learning().last_evaluation!, sessions: 4, required_sessions: 60 } }) })}/>);
    const panel = screen.getByRole('region', { name: '일일 개선' });
    expect(within(panel).getByRole('status').textContent).toBe('다음 거래일 적용 대기');
    expect(within(panel).getByText('현재 일일 정책')).toBeTruthy();
    expect(within(panel).getByText('진입 조건 개선안')).toBeTruthy();
    expect(panel.textContent).toContain('적용 거래일 2026-10-12');
    expect(panel.textContent).toContain('신호 기준일 2026-10-12');
    expect(panel.textContent).toContain('최근 심사 2026-10-12');
    expect(within(panel).getByText('비용 후 수익과 최근 구간 개선')).toBeTruthy();
    const record = within(panel).getByText('장기 검증 기록').closest('details');
    expect(record?.open).toBe(false);
    expect(record?.textContent).toContain('관측 4구간 · 기준 60구간');
    expect(panel.textContent).not.toContain('4 / 60');
    expect(within(panel).getByRole('table', { name: '현재 전략과 후보의 평가 결과' }).closest('details')).toBe(record);
  });

  it.each([['keep', '현재 정책 유지'], ['blocked', '적용 보류'], ['error', '심사 오류']] as const)
    ('shows %s and its reason without implying that a candidate was applied', (decision, label) => {
      const daily = dailyLearning({ status: decision, pending: null,
        last_decision: { ...dailyLearning().last_decision!, decision, reason: '최근 구간 개선 미확인', metrics: null } });
      render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily }) })}/>);
      const panel = screen.getByRole('region', { name: '일일 개선' });
      expect(within(panel).getAllByText(label).length).toBeGreaterThan(0);
      expect(within(panel).getByText('최근 구간 개선 미확인')).toBeTruthy();
      expect(within(panel).getByText('현재 일일 정책')).toBeTruthy();
      expect(within(panel).queryByText('적용 결정')).toBeNull();
      expect(within(panel).queryByText('다음 거래일 적용 대기')).toBeNull();
    });

  it('shows active and disabled states without inventing pending policies or decisions', () => {
    const daily = dailyLearning({ status: 'active', pending: null, last_decision: null });
    const { rerender } = render(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily }) })}/>);
    expect(screen.getByRole('region', { name: '일일 개선' }).textContent).toContain('적용 중');
    expect(screen.getByText('심사 기록 없음')).toBeTruthy();
    rerender(<ExperimentLabPanel {...props} data={experiments({ learning: learning({ daily: { ...daily, enabled: false } }) })}/>);
    expect(within(screen.getByRole('region', { name: '일일 개선' })).getByRole('status').textContent).toBe('중지');
  });

  it('marks a retained daily state unverified after a failed refresh and preserves its facts', () => {
    render(<ExperimentLabPanel {...props} error="운용실을 불러오지 못했습니다." data={experiments({ learning: learning({ daily: dailyLearning({ status: 'active' }) }) })}/>);
    const panel = screen.getByRole('region', { name: '일일 개선' });
    expect(within(panel).getByRole('status').textContent).toBe('조회 지연 · 이전 상태');
    expect(within(panel).getByRole('status').className).not.toContain('lab-badge-active');
    expect(within(panel).getByText('현재 일일 정책')).toBeTruthy();
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
    expect(result.current.error).toBe('운용실을 불러오지 못했습니다.');
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
    const data = { ...experiments({ runs: [experimentRun()], orders: [experimentOrder()], autonomy: autonomy(), learning: learning() }), additional: 'allowed' };
    expect(readExperiments(data)).toBe(data);
  });
  it.each([
    { ...experiments(), environment: 'real' },
    { ...experiments(), llm: null },
    { ...experiments(), updated_at: 'invalid' },
    { ...experiments(), strategies: [{ ...experiments().strategies[0], selectable: 'false' }] },
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

  it.each([
    null, { ...learning(), enabled: 'yes' }, { ...learning(), status: 'ready' },
    { ...learning(), champion: { ...learning().champion, adopted_at: 'invalid' } },
    { ...learning(), challenger: { ...learning().challenger, sessions: -1 } },
    { ...learning(), challenger: { ...learning().challenger, sessions: 1.5 } },
    { ...learning(), challenger: { ...learning().challenger, started_at: 'invalid' } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, as_of: '2026-02-31' } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, required_sessions: 0 } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, champion_return_pct: Infinity } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, challenger_return_pct: '1.25' } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, champion_drawdown_pct: NaN } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, closed_trades: '3' } },
    { ...learning(), last_evaluation: { ...learning().last_evaluation, phase: 'live' } },
    { ...learning(), last_change: { at: 'invalid', from: 'rules', to: 'llm', reason: '' } },
  ])('rejects malformed learning facts without coercing numbers', value => {
    expect(() => readExperiments({ ...experiments(), learning: value })).toThrow('학습 운용 응답 형식 확인 필요');
  });

  it('accepts optional owned performance, absent comparisons and unknown statistical fields', () => {
    for (const account of [undefined, null, learningAccount(), learningAccount({ comparisons: null }), learningAccount({ comparisons: undefined })]) {
      const data = experiments({ learning: learning({ account }) });
      expect(readExperiments(data)).toBe(data);
    }
    const data = experiments({ learning: learning() });
    Object.assign(data.learning!.last_evaluation!, { phase: 'paper_provisional', statistics: { combined_p_value: .02 } });
    expect(readExperiments(data)).toBe(data);
  });

  it('accepts optional daily learning, nullable initial policy dates and decisions without metrics', () => {
    for (const daily of [undefined, dailyLearning(), dailyLearning({ active: null, pending: null, last_decision: null }),
      dailyLearning({ active: { id: 'initial', name: '초기 정책', adopted_at: null, signal_day: null, effective_day: null },
        pending: { ...dailyLearning().pending!, prepared_at: null }, last_decision: { ...dailyLearning().last_decision!,
          candidate_name: null, change_family: null, metrics: null } })]) {
      const data = experiments({ learning: learning({ daily }) });
      expect(readExperiments(data)).toBe(data);
    }
  });

  it.each([
    null, { ...dailyLearning(), enabled: 'true' }, { ...dailyLearning(), status: 'complete' },
    { ...dailyLearning(), active: { ...dailyLearning().active, adopted_at: '2026-02-31T00:00:00Z' } },
    { ...dailyLearning(), active: { ...dailyLearning().active, signal_day: '2026-02-31' } },
    { ...dailyLearning(), active: { ...dailyLearning().active, effective_day: '2026-10-12T00:00:00Z' } },
    { ...dailyLearning(), pending: { ...dailyLearning().pending, signal_day: null } },
    { ...dailyLearning(), pending: { ...dailyLearning().pending, prepared_at: 'invalid' } },
    { ...dailyLearning(), last_decision: { ...dailyLearning().last_decision, as_of: '2026-02-31' } },
    { ...dailyLearning(), last_decision: { ...dailyLearning().last_decision, decision: 'promote' } },
    { ...dailyLearning(), last_decision: { ...dailyLearning().last_decision, candidate_name: 1 } },
    { ...dailyLearning(), error: false },
  ])('rejects malformed daily policy status and dates', daily => {
    expect(() => readExperiments({ ...experiments(), learning: { ...learning(), daily } })).toThrow('일일 개선 응답 형식 확인 필요');
  });

  it.each([
    [], { basis: 'development_only' },
    { ...dailyLearning().last_decision!.metrics, basis: 'confirmed_performance' },
    { ...dailyLearning().last_decision!.metrics, intervals: 1.5 },
    { ...dailyLearning().last_decision!.metrics, recent_intervals: -1 },
    { ...dailyLearning().last_decision!.metrics, champion: { ...dailyLearning().last_decision!.metrics!.champion, net_return_pct: Infinity } },
    { ...dailyLearning().last_decision!.metrics, candidate: { ...dailyLearning().last_decision!.metrics!.candidate, max_drawdown_pct: '1' } },
    { ...dailyLearning().last_decision!.metrics, delta: { stress_return_pct: NaN } },
    { ...dailyLearning().last_decision!.metrics, checks: { normal_improvement: 1 } },
    { ...dailyLearning().last_decision!.metrics, loss_reduction_only: 'false' },
  ])('rejects nonfinite or malformed daily development metrics', metrics => {
    const daily = { ...dailyLearning(), last_decision: { ...dailyLearning().last_decision, metrics } };
    expect(() => readExperiments({ ...experiments(), learning: { ...learning(), daily } })).toThrow('일일 개선 응답 형식 확인 필요');
  });

  it.each([
    [], { ...learningAccount(), as_of: 'invalid' }, { ...learningAccount(), equity: 'NaN' },
    { ...learningAccount(), cash: 0 }, { ...learningAccount(), return_pct: '2.5' },
    { ...learningAccount(), return_pct: Infinity }, { ...learningAccount(), max_drawdown_pct: -1 },
    { ...learningAccount(), observations: 1.5 }, { ...learningAccount(), basis: 'whole_account' },
    { ...learningAccount(), risk: null }, { ...learningAccount(), risk: { ...learningAccount().risk, active: 'false' } },
    { ...learningAccount(), risk: { ...learningAccount().risk, limit_pct: 0 } },
    { ...learningAccount(), risk: { ...learningAccount().risk, limit_pct: 101 } },
    { ...learningAccount(), risk: { ...learningAccount().risk, drawdown_pct: NaN } },
    { ...learningAccount(), risk: { ...learningAccount().risk, triggered_at: 'invalid' } },
  ])('rejects invalid owned account numbers and risk state', account => {
    expect(() => readExperiments({ ...experiments(), learning: { ...learning(), account } })).toThrow('AI 운용 성과 형식 확인 필요');
  });

  it.each([
    [], { ...learningAccount().comparisons, baseline_return_pct: Infinity },
    { ...learningAccount().comparisons, market_return_pct: '3' },
    { ...learningAccount().comparisons, ai_return_pct: Infinity },
    { ...learningAccount().comparisons, ai_return_pct: '1' },
    { ...learningAccount().comparisons, start_day: '2026-02-31' },
    { ...learningAccount().comparisons, start_day: '2026-10-05T00:00:00Z' },
    { ...learningAccount().comparisons, cash_return_pct: 1 },
    { ...learningAccount().comparisons, status: 'unknown' },
    { ...learningAccount().comparisons, basis: 'real_fills' },
    { ...learningAccount().comparisons, market_name: 'KOSDAQ' },
    { ...learningAccount().comparisons, error: 123 },
  ])('rejects invalid benchmark comparison facts', comparisons => {
    expect(() => readExperiments({ ...experiments(), learning: { ...learning(), account: { ...learningAccount(), comparisons } } })).toThrow('AI 비교 성과 형식 확인 필요');
  });
});
