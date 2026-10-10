import { useEffect, useMemo, useState } from 'react';
import type { ExperimentCommand, ExperimentData, ExperimentPolicy } from '../experiments';
import { number, percent, signClass, timestamp } from '../format';
import type { AccountChoice } from '../types';
import useExperiments from '../useExperiments';
import StyledSelect from './StyledSelect';
import '../experiment-lab.css';

interface Props {
  data: ExperimentData | null; loading: boolean; pending: ExperimentCommand['action'] | null; error: string | null;
  accounts: AccountChoice[]; onRefresh: () => void; onExecute: (command: ExperimentCommand) => Promise<boolean>;
  onSelectSymbol: (symbol: string) => void;
}
const emptyPolicy: ExperimentPolicy = { account_id: null, budget: null, order_cap: null, daily_buy_limit: null, execution_strategy: null };
const actions = { buy: '매수', hold: '관망', avoid: '제외' };
const reasons: Record<string, string> = {
  signal: '진입 조건 충족', no_trend: '추세 조건 미충족', no_pullback: '조정 조건 미충족',
  no_recovery: '회복 조건 미충족', no_breakout: '돌파 조건 미충족',
  no_relative_strength: '시장 대비 강세 조건 미충족', missing_benchmark: '비교 지수 자료 확인 필요',
  insufficient_history: '과거 일봉 부족', invalid_calendar: '거래일 확인 필요',
  invalid_or_missing_bar: '일봉 누락·오류 확인 필요', llm_unconfigured: 'LLM 연결 확인 필요',
  llm_pending: 'LLM 분석 대기', learned_policy_conditions: '채택 정책의 종목 조건·순위 충족',
};
const states: Record<string, string> = {
  starting: '시작 중', healthy: '정상 운용', degraded: '자동 복구 중', attention: '확인 필요',
  idle: '대기', running: '실행 중', paused: '일시 정지', disabled: '중지', stopped: '중지',
  waiting: '대기', waiting_market: '장 시작 대기', outside_market: '장외 대기', error: '확인 필요',
  queued: '주문 대기', pending: '처리 중', submitting: '전송 중', submitted: '접수', accepted: '접수',
  open: '미체결', partial: '부분 체결', partially_filled: '부분 체결', filled: '체결 완료',
  cancelled: '취소', canceled: '취소', rejected: '거절', unknown: '결과 미확정', cancel_pending: '취소 확인 중',
  complete: '완료', completed: '완료', ready: '완료', failed: '실패', skipped: '건너뜀',
};
const statusLabel = (state: string) => states[state] ?? state;
const learningStates = { disabled: '중지', waiting: '대기', researching: '후보 연구 중', evaluating: '후보 평가 중', error: '확인 필요' };
const learningDecisions: Record<string, string> = { promote: '후보 채택', promoted: '후보 채택', keep: '현재 전략 유지',
  retained: '현재 전략 유지', waiting: '평가 중', wait: '평가 중', reject: '후보 제외', rejected: '후보 제외',
  invalid: '평가 무효', rollback: '이전 전략 복귀' };
const comparisonStates: Record<string, string> = { ready: '비교 모형', waiting: '비교 관측 대기', invalid: '비교 자료 확인 필요' };
const magnitudePercent = (value: number) => percent(value).replace(/^\+/, '');
const PAGE_SIZE = 10;

function Pager({ page, pages, count, label, onChange }: { page: number; pages: number; count: number; label: string; onChange: (page: number) => void }) {
  return pages > 1 ? <div className="lab-pagination"><span>{number(count)}건 · {page} / {pages}</span>
    <nav aria-label={label}><button type="button" disabled={page === 1} onClick={() => onChange(page - 1)}>이전</button>
      <button type="button" disabled={page === pages} onClick={() => onChange(page + 1)}>다음</button></nav></div> : null;
}

function ResolveOrder({ id, name, disabled, onExecute }: { id: string; name: string; disabled: boolean; onExecute: Props['onExecute'] }) {
  const [brokerId, setBrokerId] = useState('');
  const [branchId, setBranchId] = useState('');
  return <details className="lab-resolve"><summary>접수 결과 대조</summary><form onSubmit={event => {
    event.preventDefault();
    if (brokerId.trim() && branchId.trim() && !disabled) void onExecute({ action: 'resolve', id, broker_order_id: brokerId.trim(), branch_id: branchId.trim() });
  }}><label>증권사 주문번호<input aria-label={`${name} 증권사 주문번호`} type="text" value={brokerId} required disabled={disabled} onChange={event => setBrokerId(event.target.value)}/></label>
    <label>주문 지점번호<input aria-label={`${name} 주문 지점번호`} type="text" value={branchId} required disabled={disabled} onChange={event => setBranchId(event.target.value)}/></label>
    <button className="lab-button" type="submit" disabled={disabled || !brokerId.trim() || !branchId.trim()}>주문 대조</button></form></details>;
}

export function ExperimentLabPanel({ data, loading, pending, error, accounts, onRefresh, onExecute, onSelectSymbol }: Props) {
  const [policy, setPolicy] = useState<ExperimentPolicy>(data?.policy ?? emptyPolicy);
  const savedPolicy = JSON.stringify(data?.policy ?? emptyPolicy);
  useEffect(() => { setPolicy(JSON.parse(savedPolicy) as ExperimentPolicy); }, [savedPolicy]);
  const [runId, setRunId] = useState('');
  const [strategyId, setStrategyId] = useState('');
  const [signalPage, setSignalPage] = useState(1);
  const [orderPage, setOrderPage] = useState(1);
  const runs = useMemo(() => [...(data?.runs ?? [])].sort((left, right) => right.created_at.localeCompare(left.created_at)), [data?.runs]);
  const run = runs.find(item => item.id === runId) ?? runs[0];
  const signals = useMemo(() => (run?.signals ?? []).filter(item => !strategyId || item.strategy_id === strategyId), [run, strategyId]);
  const signalPages = Math.max(1, Math.ceil(signals.length / PAGE_SIZE));
  const currentSignalPage = Math.min(signalPage, signalPages);
  const orders = useMemo(() => [...(data?.orders ?? [])].sort((left, right) => right.created_at.localeCompare(left.created_at)), [data?.orders]);
  const orderPages = Math.max(1, Math.ceil(orders.length / PAGE_SIZE));
  const currentOrderPage = Math.min(orderPage, orderPages);
  const learning = data?.learning;
  const ownedAccount = learning?.account;
  const comparisons = ownedAccount?.comparisons;
  const label = (id: string) => id === 'all-strategies-v1' ? learning?.enabled ? '자동 전략 운용' : '전체 전략 균등' : data?.strategies.find(strategy => strategy.id === id)?.label ?? id;
  const locked = !!pending || !data;
  const positive = (value: string | null) => value !== null && /^\d+(?:\.\d+)?$/.test(value) && Number(value) > 0;
  const configured = !!data?.policy.account_id && !!data?.policy.execution_strategy
    && [data?.policy.budget, data?.policy.order_cap, data?.policy.daily_buy_limit].every(value => positive(value ?? null));
  const complete = !!policy.account_id && !!policy.execution_strategy && [policy.budget, policy.order_cap, policy.daily_buy_limit].every(positive);
  const dirty = JSON.stringify(policy) !== savedPolicy;
  const updated = timestamp(data?.updated_at ?? null);
  const warning = error || data?.error;
  const autonomy = data?.autonomy;
  const performance = autonomy?.performance;
  const issues = autonomy?.issues.filter(issue => issue.state === 'open') ?? [];
  const change = (key: keyof ExperimentPolicy, value: string) => setPolicy(previous => ({ ...previous, [key]: value || null }));
  const executionLabel = data?.policy.execution_strategy ? label(data.policy.execution_strategy) : autonomy ? '자동 설정 중' : '전략 미설정';
  const openOrderCount = orders.filter(order => order.filled_quantity < order.quantity && !['cancelled', 'canceled', 'rejected', 'failed', 'filled'].includes(order.status)).length;
  const unresolvedOrders = orders.some(order => !['cancelled', 'canceled', 'rejected', 'failed', 'filled'].includes(order.status));
  const riskResetBlocked = !!data?.automation.enabled || !!data?.positions.some(position => position.quantity > 0) || unresolvedOrders;

  return <div className="experiment-lab" aria-busy={loading && !data}>
    <section className="lab-control" aria-labelledby="lab-control-title">
      <div className="panel-heading lab-heading"><div className="panel-title"><h2 id="lab-control-title">모의 운용</h2>
        <span className={`lab-badge ${data?.automation.enabled ? 'lab-badge-active' : ''}`} role="status">{data ? data.automation.enabled ? '자동 주문 켜짐' : '자동 주문 꺼짐' : loading ? '조회 중' : '확인 필요'}</span></div>
        <button type="button" className="lab-button" disabled={loading || !!pending} onClick={onRefresh}>상태 새로고침</button></div>
      <div className="lab-status-line"><span>{data ? statusLabel(autonomy?.status ?? data.automation.state) : '—'}</span><span>{executionLabel}</span>
        <time dateTime={updated.dateTime}>{updated.text}</time></div>
      {data?.automation.pause_reason ? <p className="lab-pause-reason">{data.automation.pause_reason}</p> : null}
      {warning && !issues.some(issue => issue.message === warning) ? <div className="lab-error" role="alert">{warning}</div> : null}
      {autonomy?.error && autonomy.error !== warning && !issues.some(issue => issue.message === autonomy.error) ? <div className="lab-error" role="alert">{autonomy.error}</div> : null}
      {issues.length ? <div className="lab-issues" aria-label="확인할 사항">{issues.map(issue => <article key={issue.id} className={issue.blocking ? 'lab-issue lab-issue-blocking' : 'lab-issue'}>
        <div><span className="lab-issue-state">{issue.blocking ? '응답 필요' : '확인 사항'}</span><p>{issue.message}</p>
          {issue.question && issue.question !== issue.message ? <p className="lab-issue-question">{issue.question}</p> : null}</div>
        {issue.question ? <div className="lab-issue-actions"><button type="button" className="lab-button" disabled={locked} onClick={() => { void onExecute({ action: 'answer', id: issue.id, answer: 'retry' }); }}>다시 시도</button>
          <button type="button" className="lab-button" disabled={locked} onClick={() => { void onExecute({ action: 'answer', id: issue.id, answer: 'keep_paused' }); }}>중지 유지</button></div> : null}
      </article>)}</div> : null}
      {autonomy ? <>
        <dl className="lab-stats lab-account-performance" aria-label="모의계좌 성과">
          <div><dt>총 평가금액</dt><dd>{number(performance?.total_value)}<span>원</span></dd></div>
          <div><dt>누적 수익률</dt><dd className={signClass(performance?.return_pct)}>{percent(performance?.return_pct)}</dd></div>
          <div><dt>최대 낙폭</dt><dd>{percent(performance?.max_drawdown_pct)}</dd></div>
          <div><dt>예수금</dt><dd>{number(performance?.cash)}<span>원</span></dd></div>
        </dl>
        <div className="lab-performance-basis"><span>관찰 시작 평가액 {number(performance?.baseline)}원</span><span>기록 {number(performance?.observations)}회</span>
          <span>전략 {number(data?.strategies.length)}개 · 보유 {number(data?.positions.length)}종목 · 진행 주문 {number(openOrderCount)}건</span></div>
        <dl className="lab-health-times" aria-label="최근 자동 운용 확인">
          {([['상태 확인', autonomy.last_heartbeat_at], ['계좌 확인', autonomy.last_account_at], ['최근 분석', runs[0]?.created_at ?? null],
            ['최근 주문', orders[0]?.created_at ?? null], ...(autonomy.next_retry_at ? [['다음 재시도', autonomy.next_retry_at]] : [])] as [string, string | null][])
            .map(([title, value]) => <div key={title}><dt>{title}</dt><dd><time dateTime={value ?? undefined}>{timestamp(value).text}</time></dd></div>)}
        </dl>
      </> : <dl className="lab-stats" aria-label="실험 집계">
        <div><dt>전략</dt><dd>{number(data?.strategies.length)}</dd></div>
        <div><dt>분석 실행</dt><dd>{number(data?.runs.length)}</dd></div>
        <div><dt>보유 종목</dt><dd>{number(data?.positions.length)}</dd></div>
        <div><dt>진행 주문</dt><dd>{data ? number(openOrderCount) : '—'}</dd></div>
      </dl>}
      {autonomy && !issues.length ? <p className="lab-no-issues">확인할 사항 없음</p> : null}
      <div className="lab-actions">
        <button type="button" className="lab-button lab-primary" disabled={locked || data?.busy} onClick={() => { void onExecute({ action: 'analyze' }); }}>{pending === 'analyze' || data?.busy ? '분석 중' : '지금 분석'}</button>
        {data?.automation.enabled ? <button type="button" className="lab-button lab-pause" disabled={locked} onClick={() => { void onExecute({ action: 'pause' }); }}>{pending === 'pause' ? '정지 중' : '신규 주문 정지'}</button>
          : <button type="button" className="lab-button" disabled={locked || (!autonomy && !configured) || dirty || !!warning} onClick={() => { void onExecute({ action: 'start' }); }}>{pending === 'start' ? '시작 중' : ownedAccount?.risk.active ? '보호 청산 재개' : '모의 자동 주문 시작'}</button>}
        <button type="button" className="lab-button" disabled={locked} onClick={() => { void onExecute({ action: 'reconcile' }); }}>{pending === 'reconcile' ? '확인 중' : '체결 확인'}</button>
      </div>
      <details className="lab-policy">
        <summary>고급 운용 설정{dirty ? <span className="lab-unsaved">저장 전</span> : null}</summary>
        <form onSubmit={event => { event.preventDefault(); if (complete && dirty && !locked && !data?.automation.enabled) void onExecute({ action: 'configure', policy }); }}>
          <div className="lab-policy-fields">
            <StyledSelect label="모의계좌" value={policy.account_id ?? ''} disabled={locked || data?.automation.enabled}
              options={[{ value: '', label: '계좌 선택' }, ...accounts.map(account => ({ value: account.id, label: account.name, disabled: !account.configured }))]}
              onChange={value => change('account_id', value)}/>
            <StyledSelect label="주문 실행 전략" value={policy.execution_strategy ?? ''} disabled={locked || data?.automation.enabled}
              options={[{ value: '', label: '전략 선택' }, ...(autonomy || policy.execution_strategy === 'all-strategies-v1' ? [{ value: 'all-strategies-v1', label: label('all-strategies-v1') }] : []), ...(data?.strategies ?? []).filter(strategy => strategy.selectable !== false).map(strategy => ({ value: strategy.id, label: strategy.label }))]}
              onChange={value => change('execution_strategy', value)}/>
            {([['budget', '실험 예산'], ['order_cap', '건당 매수 한도'], ['daily_buy_limit', '일일 매수 한도']] as const).map(([key, title]) => <label key={key} className="lab-money-field">
              <span>{title}</span><div><input aria-label={title} inputMode="decimal" type="text" pattern="[0-9]+([.][0-9]+)?" required value={policy[key] ?? ''}
                disabled={locked || data?.automation.enabled} onChange={event => change(key, event.target.value)}/><span>원</span></div>
            </label>)}
          </div>
          <div className="lab-policy-footer"><button type="submit" className="lab-button" disabled={!complete || !dirty || locked || data?.automation.enabled}>{pending === 'configure' ? '저장 중' : '설정 저장'}</button>
            {data?.automation.enabled ? <span>신규 주문 정지 후 변경</span> : null}</div>
        </form>
      </details>
    </section>

    {learning ? <section className="lab-learning" aria-labelledby="lab-learning-title">
      <div className="panel-heading lab-heading"><h2 id="lab-learning-title">학습·전략 교체</h2>
        <span className={`lab-badge ${learning.enabled ? 'lab-badge-active' : ''}`}>{learningStates[learning.status]}</span></div>
      {ownedAccount ? <div className="lab-owned-account" role="group" aria-label="AI 운용 성과">
        <div className="lab-owned-heading"><h3>AI 운용 성과</h3><time dateTime={ownedAccount.as_of}>
          {ownedAccount.as_of.length === 10 ? ownedAccount.as_of : timestamp(ownedAccount.as_of).text}</time></div>
        <dl className="lab-owned-metrics"><div><dt>평가액</dt><dd>{number(ownedAccount.equity)}원</dd></div>
          <div><dt>누적 수익률</dt><dd className={signClass(ownedAccount.return_pct)}>{percent(ownedAccount.return_pct)}</dd></div>
          <div><dt>최대 낙폭</dt><dd>{magnitudePercent(ownedAccount.max_drawdown_pct)}</dd></div>
          <div><dt>현금</dt><dd>{number(ownedAccount.cash)}원</dd></div></dl>
        {comparisons ? <div className="lab-owned-comparisons">
          <div className="lab-owned-basis">{comparisonStates[comparisons.status]} · <time dateTime={comparisons.start_day}>{comparisons.start_day}</time>부터 · 동기간 AI {percent(comparisons.ai_return_pct)}</div>
          <table className="lab-learning-table" aria-label="AI 운용과 비교 기준 수익률"><thead><tr><th scope="col">기준</th><th scope="col">수익률</th><th scope="col">AI 초과</th></tr></thead>
            <tbody>{([['최초 정책', comparisons.baseline_return_pct], ['현금', comparisons.cash_return_pct], [comparisons.market_name, comparisons.market_return_pct]] as const).map(([title, value]) => {
              const excess = comparisons.status === 'ready' && value !== null && comparisons.ai_return_pct !== null ? comparisons.ai_return_pct - value : null;
              return <tr key={title}><th scope="row">{title}</th><td className={signClass(value)}>{percent(value)}</td>
                <td className={signClass(excess)}>{excess === null ? '—' : `${number(excess, true)}%p`}</td></tr>;
            })}</tbody></table>
          {comparisons.error ? <p className="lab-learning-reason" role="alert">{comparisons.error}</p> : null}
        </div> : null}
        <div className={`lab-owned-risk ${ownedAccount.risk.active ? 'lab-owned-risk-active' : ''}`}>
          <span>{ownedAccount.risk.active ? '손실 보호 발동' : '손실 보호 정상'}</span>
          <span>보호 낙폭 {magnitudePercent(ownedAccount.risk.drawdown_pct)} / 기준 {magnitudePercent(ownedAccount.risk.limit_pct)}</span>
          {ownedAccount.risk.active ? <><button type="button" className="lab-button" disabled={locked || loading || !!warning || riskResetBlocked}
            onClick={() => { void onExecute({ action: 'reset_risk' }); }}>{pending === 'reset_risk' ? '보호 해제 중' : '손실 보호 해제'}</button>
            {riskResetBlocked ? <span>주문 정지·보유 및 미체결 정리 필요</span> : null}</> : null}
        </div>
      </div> : null}
      <dl className="lab-learning-strategies"><div><dt>현재 전략</dt><dd>{learning.champion.name}</dd>
        {learning.champion.adopted_at ? <dd className="lab-learning-time">채택 <time dateTime={learning.champion.adopted_at}>{timestamp(learning.champion.adopted_at).text}</time></dd> : null}</div>
        <div><dt>평가 후보</dt><dd>{learning.challenger?.name ?? '없음'}</dd>{learning.challenger ? <dd className="lab-learning-time">
          <time dateTime={learning.challenger.started_at}>{timestamp(learning.challenger.started_at).text}</time> · 수익률 {number(learning.challenger.sessions)}구간</dd> : null}</div></dl>
      {learning.last_evaluation ? <div className="lab-learning-evaluation">
        <div className="lab-learning-result"><strong>{learningDecisions[learning.last_evaluation.decision] ?? learning.last_evaluation.decision}</strong>
          {learning.last_evaluation.phase === 'paper_provisional' ? <span>모의 비교</span> : null}
          <span>{number(learning.last_evaluation.sessions)} / {number(learning.last_evaluation.required_sessions)}구간 · 완결 {number(learning.last_evaluation.closed_trades)}건</span>
          <time dateTime={learning.last_evaluation.as_of}>{learning.last_evaluation.as_of.length === 10 ? learning.last_evaluation.as_of : timestamp(learning.last_evaluation.as_of).text}</time></div>
        <table className="lab-learning-table" aria-label="현재 전략과 후보의 평가 결과"><thead><tr><th scope="col">평가 지표</th><th scope="col">현재</th><th scope="col">후보</th></tr></thead>
          <tbody><tr><th scope="row">수익률</th><td className={signClass(learning.last_evaluation.champion_return_pct)}>{percent(learning.last_evaluation.champion_return_pct)}</td>
            <td className={signClass(learning.last_evaluation.challenger_return_pct)}>{percent(learning.last_evaluation.challenger_return_pct)}</td></tr>
            <tr><th scope="row">최대 낙폭</th><td>{percent(learning.last_evaluation.champion_drawdown_pct)}</td><td>{percent(learning.last_evaluation.challenger_drawdown_pct)}</td></tr></tbody></table>
        <p className="lab-learning-reason">{learning.last_evaluation.reason}</p>
      </div> : <p className="lab-learning-reason lab-subtle">평가 기록 없음</p>}
      {learning.last_change ? <div className="lab-learning-change"><span>최근 교체</span><time dateTime={learning.last_change.at}>{timestamp(learning.last_change.at).text}</time>
        <strong>{label(learning.last_change.from)} → {label(learning.last_change.to)}</strong><p>{learning.last_change.reason}</p></div> : null}
      {learning.error ? <p className="lab-error" role="alert">{learning.error}</p> : null}
    </section> : null}

    {autonomy?.daily_reports.length ? <section aria-labelledby="lab-daily-title"><div className="panel-heading lab-heading"><h2 id="lab-daily-title">일별 운용</h2></div>
      <ul className="lab-daily-reports">{[...autonomy.daily_reports].sort((left, right) => right.date.localeCompare(left.date)).slice(0, 7).map(report => <li key={report.date}>
        <time dateTime={report.date}>{report.date}</time><span>{number(report.total_value)}원</span><strong className={signClass(report.return_pct)}>{percent(report.return_pct)}</strong>
        <span>주문 {number(report.orders)} · 체결 {number(report.filled_orders)} · 확인 {number(report.issues)}</span>
      </li>)}</ul></section> : null}

    <section className="lab-strategies" aria-labelledby="lab-strategy-title">
      <div className="panel-heading lab-heading"><h2 id="lab-strategy-title">전략 비교</h2><div className="lab-llm-state"><span className={`lab-dot ${data?.llm.configured && !data.llm.error ? 'lab-dot-on' : ''}`}/>
        <span>{data?.llm.provider === 'codex' ? 'Codex' : 'LLM'} {data?.llm.configured ? data.llm.error ? '확인 필요' : '연결 설정됨' : '미설정'}</span>{data?.llm.model ? <span>{data.llm.model}</span> : null}</div></div>
      {data?.llm.error ? <p className="lab-error" role="alert">{data.llm.error}</p> : null}
      {data?.metrics.some(metric => metric.shadow_closed !== undefined) ? <p className="lab-comparison-basis">가상 평균 · 신호일 동일가중 · 비용 후</p> : null}
      <div className="lab-strategy-grid">{data?.strategies.map(strategy => {
        const metric = data.metrics.find(item => item.strategy_id === strategy.id);
        return <article className={`lab-strategy-card ${data.policy.execution_strategy === strategy.id ? 'lab-strategy-selected' : ''}`} key={strategy.id}>
          <div className="lab-card-title"><h3>{strategy.label}</h3>{data.policy.execution_strategy === strategy.id ? <span className="lab-tag">주문 전략</span> : null}</div>
          <p>{strategy.description}</p><dl className="lab-live-metrics"><div><dt>비용 추정 실현손익</dt><dd className={signClass(metric?.realized_pnl)}>{number(metric?.realized_pnl, true)}<span>원</span></dd></div>
            <div><dt>완결 거래</dt><dd>{number(metric?.closed_trades)}</dd></div><div><dt>보유</dt><dd>{number(metric?.open_positions)}</dd></div></dl>
          <dl className="lab-shadow-metrics"><div><dt>가상 완결</dt><dd>{number(metric?.shadow_closed)}</dd></div>
            <div><dt>가상 평균 <span>기본 / 높은 비용</span></dt><dd><span className={signClass(metric?.shadow_net_pct)}>{percent(metric?.shadow_net_pct)}</span>
              <span className="lab-metric-divider">/</span><span className={signClass(metric?.shadow_stress_pct)}>{percent(metric?.shadow_stress_pct)}</span></dd></div></dl>
          <p className="lab-shadow-counts"><span>진입 대기 {number(metric?.shadow_pending)}</span><span>관찰 {number(metric?.shadow_open)}</span><span>미확정 {number(metric?.shadow_unknown)}</span><span>제외 {number(metric?.shadow_excluded)}</span>
            {metric?.shadow_version_count !== undefined ? <span>버전 {number(metric.shadow_version_count)}{metric.shadow_version_count > 1 ? '개 합산' : ''}</span> : null}</p>
          <span className="lab-version" title={strategy.version}>{strategy.version.length > 12 ? strategy.version.slice(0, 12) : strategy.version}</span>
        </article>;
      })}</div>
      {!data?.strategies.length ? <p className="lab-empty">{loading ? '전략 조회 중' : '전략 없음'}</p> : null}
    </section>

    <section aria-labelledby="lab-signals-title">
      <div className="panel-heading lab-heading"><h2 id="lab-signals-title">분석 기록</h2><span className="lab-subtle">{run ? `${run.as_of ?? '기준일 미확인'} · ${statusLabel(run.status)}` : '실행 기록 없음'}</span></div>
      {runs.length ? <div className="lab-filters"><StyledSelect label="분석 실행" value={run?.id ?? ''} options={runs.map(item => ({ value: item.id, label: timestamp(item.created_at).text }))}
        onChange={value => { setRunId(value); setSignalPage(1); }}/><StyledSelect label="분석 전략" value={strategyId}
          options={[{ value: '', label: '전체 전략' }, ...(data?.strategies ?? []).map(strategy => ({ value: strategy.id, label: strategy.label }))]}
          onChange={value => { setStrategyId(value); setSignalPage(1); }}/></div> : null}
      {run?.error ? <p className="lab-error" role="alert">{run.error}</p> : null}
      {signals.length ? <ul className="lab-signal-list">{signals.slice((currentSignalPage - 1) * PAGE_SIZE, currentSignalPage * PAGE_SIZE).map((signal, index) => <li key={`${signal.strategy_id}-${signal.symbol}-${index}`}>
        <div className="lab-signal-heading"><div><button type="button" className="market-stock-link stock-name" aria-label={`${signal.name || signal.symbol} 일봉 차트 보기`}
          onClick={() => onSelectSymbol(signal.symbol)}>{signal.name || signal.symbol}</button><span className="stock-symbol">{signal.symbol} · {label(signal.strategy_id)}</span></div>
          <div className="lab-signal-score"><span className={`lab-action lab-action-${signal.action}`}>{actions[signal.action]}</span><span>{signal.score === null ? '점수 없음' : number(signal.score)}</span></div></div>
        <details className="lab-evidence"><summary>판단 근거</summary><p>{reasons[signal.reason] ?? (signal.reason || '근거 없음')}</p>{signal.evidence_ids.length ? <ul aria-label="근거 식별자">{signal.evidence_ids.map((evidence, index) => <li key={`${evidence}-${index}`}>{evidence}</li>)}</ul> : null}</details>
      </li>)}</ul> : <p className="lab-empty">{run ? '표시할 판단 없음' : '분석 기록 없음'}</p>}
      <Pager page={currentSignalPage} pages={signalPages} count={signals.length} label="분석 기록 페이지" onChange={setSignalPage}/>
    </section>

    <section aria-labelledby="lab-orders-title">
      <div className="panel-heading lab-heading"><h2 id="lab-orders-title">주문·체결</h2><span className="lab-subtle">{data ? `${number(orders.length)}건` : '—'}</span></div>
      {orders.length ? <table className="lab-table" aria-label="실험 주문과 체결"><thead><tr><th scope="col">종목 / 전략</th><th scope="col">주문</th><th scope="col">체결</th><th scope="col">상태</th></tr></thead>
        <tbody>{orders.slice((currentOrderPage - 1) * PAGE_SIZE, currentOrderPage * PAGE_SIZE).map(order => {
          const cancelable = !!order.order_id && order.filled_quantity < order.quantity && ['submitted', 'partial'].includes(order.status);
          return <tr key={order.id}><td><button type="button" className="market-stock-link stock-name" aria-label={`${order.name || order.symbol} 주문 종목 차트 보기`}
            onClick={() => onSelectSymbol(order.symbol)}>{order.name || order.symbol}</button><span className="stock-symbol">{order.symbol} · {label(order.strategy_id)}</span>
            <time className="lab-row-time" dateTime={order.created_at}>{timestamp(order.created_at).text}</time></td>
            <td><span className={`lab-action lab-action-${order.side}`}>{order.side === 'buy' ? '매수' : '매도'}</span> {number(order.quantity)}주<span className="lab-cell-sub">지정가 {number(order.limit_price)}원</span></td>
            <td><span className="lab-mobile-label">체결</span>{number(order.filled_quantity)} / {number(order.quantity)}주<span className="lab-cell-sub">평균 {number(order.average_price)}원</span></td>
            <td><span className="lab-order-status">{statusLabel(order.status)}</span>{order.order_id ? <span className="lab-cell-sub">{order.order_id}</span> : null}
              {order.error ? <span className="lab-row-error">{order.error}</span> : null}
              {cancelable ? <button type="button" className="lab-cancel" disabled={locked} aria-label={`${order.name || order.symbol} 미체결 취소`}
                onClick={() => { void onExecute({ action: 'cancel', order_id: order.id }); }}>미체결 취소</button> : null}
              {order.status === 'unknown' ? <ResolveOrder id={order.id} name={order.name || order.symbol} disabled={locked} onExecute={onExecute}/> : null}</td></tr>;
        })}</tbody></table> : <p className="lab-empty">주문 기록 없음</p>}
      <Pager page={currentOrderPage} pages={orderPages} count={orders.length} label="주문 기록 페이지" onChange={setOrderPage}/>
    </section>

    {data?.positions.length ? <section aria-labelledby="lab-positions-title"><div className="panel-heading"><h2 id="lab-positions-title">실험 보유</h2></div>
      <ul className="lab-position-list">{data.positions.map(position => <li key={`${position.strategy_id}-${position.symbol}`}><div><strong>{position.name || position.symbol}</strong>
        <span className="stock-symbol">{position.symbol} · {label(position.strategy_id)}</span></div><div>{number(position.quantity)}주<span className="lab-cell-sub">평균 {number(position.average_price)}원</span></div></li>)}</ul></section> : null}
    {data?.events.length ? <section aria-labelledby="lab-events-title"><div className="panel-heading"><h2 id="lab-events-title">최근 활동</h2></div><ol className="lab-events">
      {[...data.events].sort((left, right) => right.at.localeCompare(left.at)).slice(0, 8).map((event, index) => <li key={`${event.at}-${index}`}><time dateTime={event.at}>{timestamp(event.at).text}</time><span>{event.message}</span></li>)}</ol></section> : null}
  </div>;
}

export default function ExperimentLab({ accounts, onSelectSymbol }: Pick<Props, 'accounts' | 'onSelectSymbol'>) {
  const experiments = useExperiments();
  return <ExperimentLabPanel {...experiments} accounts={accounts} onRefresh={() => { void experiments.refresh(); }} onExecute={experiments.execute} onSelectSymbol={onSelectSymbol}/>;
}
