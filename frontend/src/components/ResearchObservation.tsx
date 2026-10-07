import { useMemo, useState } from 'react';
import { number, percent, signClass, timestamp } from '../format';
import type { ObservationClass, ObservationStatus, ResearchData } from '../research';
import useResearchObservation from '../useResearchObservation';
import '../research-observation.css';

interface Props {
  data: ResearchData | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onSelectSymbol: (symbol: string) => void;
}

const PAGE_SIZE = 10;
const classifications: Record<ObservationClass, string> = {
  prospective: '전진 신호', bootstrap: '초기 이력', late: '지연 기록', timing_unverified: '시점 확인 대기',
};
const outcomes: Record<ObservationStatus, string> = {
  excluded: '평가 제외', pending_entry: '진입 대기', open: '관찰 중', closed: '완결', unknown: '미확정', fill_unverifiable: '체결 미확인',
};
const statuses: Record<ResearchData['status'], string> = {
  idle: '수집 대기', collecting: '수집 중', ready: '관찰 중', partial: '일부 확인 필요', error: '확인 필요',
};
const reasonLabels: Record<string, string> = {
  price_revision: '수정주가 변경', calendar_gap: '거래일 자료 누락', missing_entry_bar: '진입 일봉 누락',
  entry_fill_unverifiable: '진입 체결 미확인', missing_holding_bar: '보유 기간 일봉 누락',
  missing_exit_bar: '청산 일봉 누락', exit_fill_unverifiable: '청산 체결 미확인', time5_delayed: '청산 지연',
};

export function ResearchObservationPanel({ data, loading, error, onRefresh, onSelectSymbol }: Props) {
  const [page, setPage] = useState(1);
  const signals = useMemo(() => (data?.observations ?? []).filter(row => row.signal)
    .sort((left, right) => right.signal_date.localeCompare(left.signal_date) || left.symbol.localeCompare(right.symbol)), [data?.observations]);
  const pageCount = Math.max(1, Math.ceil(signals.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const rows = signals.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const updated = timestamp(data?.observed_at ?? null);
  const warning = error || data?.error;
  const state = error && data && data.status !== 'error' && data.status !== 'partial' ? '이전 결과 · 확인 필요'
    : data ? statuses[data.status] : error ? '확인 필요' : '조회 중';
  const comparison = data?.comparison;
  const paired = comparison && comparison.signal_mean !== null && comparison.control_mean !== null && comparison.edge !== null;
  const empty = warning ? '관찰 기록 확인 필요' : loading && !data ? '관찰 기록 조회 중'
    : data?.status === 'idle' ? '첫 관찰 수집 대기' : data?.status === 'collecting' ? '관찰 기록 수집 중'
      : data && data.counts.signals > 0 ? '최근 기록에 표시할 신호 없음' : '전진 신호 없음';

  return <section id="research-observation" className="research-panel" aria-labelledby="research-title" aria-busy={loading}>
    <div className="panel-heading research-heading">
      <div className="panel-title research-title"><h2 id="research-title">전진 관찰</h2><span className="research-badge">연구 중 · 미채택</span></div>
      <span className="research-order-state">자동 주문 없음</span>
    </div>
    <div className="research-meta">
      <span className={warning ? 'research-warning-state' : 'research-state'} role="status">{state}</span>
      <span>기준일 {data?.as_of ? <time dateTime={data.as_of}>{data.as_of}</time> : '—'}</span>
      {data?.observed_at ? <span>갱신 <time dateTime={updated.dateTime}>{updated.text}</time></span> : null}
    </div>
    <dl className="research-counts" aria-label="전진 신호 집계">
      <div><dt>전진 신호</dt><dd>{number(data?.counts.prospective)}</dd></div>
      <div><dt>완결</dt><dd>{number(data?.counts.closed)}</dd></div>
      <div><dt>관찰 중</dt><dd>{number(data?.counts.open)}</dd></div>
      <div><dt>미확정</dt><dd>{number(data?.counts.unknown)}</dd></div>
    </dl>
    {data && (data.counts.bootstrap > 0 || data.counts.late > 0 || (data.counts.timing_unverified ?? 0) > 0) ? <p className="research-history-counts">
      {data.counts.bootstrap > 0 ? <span>초기 이력 {number(data.counts.bootstrap)}</span> : null}
      {data.counts.late > 0 ? <span>지연 기록 {number(data.counts.late)}</span> : null}
      {(data.counts.timing_unverified ?? 0) > 0 ? <span>시점 확인 대기 {number(data.counts.timing_unverified)}</span> : null}
      <span>평가 제외</span>
    </p> : null}
    {warning ? <div className="candidate-warning"><p className="history-error research-error" role="alert">{warning}</p>
      <button type="button" className="candidate-retry" disabled={loading} onClick={onRefresh}>다시 확인</button></div> : null}
    {rows.length ? <>
      <div className="research-table-note">최근 기록 · 가상 결과 · 비용 후 · 슬리피지 편도 0.1%</div>
      <table className="research-table" aria-label="전진 관찰 신호와 가상 결과">
        <thead><tr><th scope="col">종목</th><th scope="col">신호일 / 구분</th><th scope="col">관찰 상태</th><th scope="col">진입 / 종료</th><th scope="col">가상 수익률</th></tr></thead>
        <tbody>{rows.map(row => {
          const included = row.classification === 'prospective';
          const netReturn = included && row.outcome.status === 'closed'
            ? row.outcome.returns.find(value => value.slippage === 0.001)?.net_return ?? null : null;
          const reasonCode = row.outcome.reason || row.reason;
          const reason = included && (['unknown', 'fill_unverifiable'].includes(row.outcome.status) || reasonCode === 'time5_delayed')
            ? reasonLabels[reasonCode ?? ''] || '자료 확인 필요' : null;
          return <tr key={`${row.signal_date}-${row.symbol}-${row.classification}`}>
            <td className="research-stock"><button type="button" className="market-stock-link stock-name" aria-label={`${row.name || row.symbol} 일봉 차트 보기`}
              onClick={() => onSelectSymbol(row.symbol)}>{row.name || row.symbol}</button><span className="stock-symbol">{row.symbol} · {row.board}</span></td>
            <td className="research-signal"><span className="research-mobile-label">신호일</span><time dateTime={row.signal_date}>{row.signal_date}</time>
              <span className={`research-class research-class-${row.classification}`}>{classifications[row.classification]}</span></td>
            <td className="research-outcome"><span className="research-mobile-label">관찰 상태</span>{included ? outcomes[row.outcome.status] : outcomes.excluded}
              {reason ? <span className="research-row-reason">{reason}</span> : null}</td>
            <td className="research-dates"><span className="research-mobile-label">진입 / 종료</span>
              <span>{included && row.outcome.entry_date ? <time dateTime={row.outcome.entry_date}>{row.outcome.entry_date}</time> : '—'}</span>
              <span className="research-exit">{included && row.outcome.exit_date ? <time dateTime={row.outcome.exit_date}>{row.outcome.exit_date}</time> : '—'}</span></td>
            <td className={`research-return ${signClass(netReturn)}`}><span className="research-mobile-label">가상 수익률</span>{percent(netReturn === null ? null : netReturn * 100)}</td>
          </tr>;
        })}</tbody>
      </table>
      {pageCount > 1 ? <div className="candidate-pagination research-pagination"><span>{number(signals.length)}건 · {currentPage} / {pageCount}페이지</span>
        <nav aria-label="전진 관찰 페이지"><button type="button" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>이전</button>
          <button type="button" disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)}>다음</button></nav></div> : null}
    </> : <p className="research-empty">{empty}</p>}
    {paired ? <div className="research-comparison" aria-label="완결된 신호일별 가상 비교">
      <span>가상 비교{comparison.pending ? ' · 집계 중' : ''}</span>
      <span>신호 {percent(comparison.signal_mean! * 100)}</span><span>대조 {percent(comparison.control_mean! * 100)}</span>
      <span>차이 <strong className={signClass(comparison.edge)}>{number(comparison.edge! * 100, true)}%p</strong></span>
      <span>신호일 동일가중{comparison.paired_days !== undefined ? ` · ${number(comparison.paired_days)}일` : ''}</span>
    </div> : null}
  </section>;
}

export default function ResearchObservation({ onSelectSymbol }: Pick<Props, 'onSelectSymbol'>) {
  const observation = useResearchObservation();
  return <ResearchObservationPanel {...observation} onRefresh={() => { void observation.refresh(); }} onSelectSymbol={onSelectSymbol}/>;
}
