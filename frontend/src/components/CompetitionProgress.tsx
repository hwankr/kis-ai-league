import { number, numeric, timestamp } from '../format';
import type { CompetitionProgressData, ProgressMetric } from '../competition-progress';
import useCompetitionProgress from '../useCompetitionProgress';
import '../competition-progress.css';

const shortDate = (value: string | null | undefined) => value ? value.slice(5).replace('-', '.') : '—';
function Meter({ label, metric, unit, status, hint }: {
  label: string; metric?: ProgressMetric; unit: string; status?: string; hint?: string;
}) {
  const current = numeric(metric?.value), target = numeric(metric?.target);
  const amount = unit === '원';
  const targetText = amount && target === 500_000_000 ? '5억' : number(target);
  return <div className={`competition-metric${metric?.met ? ' competition-met' : ''}`}>
    <div className="competition-label"><span>{label}</span>{metric?.met ? <span className="competition-check">달성</span> : null}</div>
    <div className="competition-value"><strong>{number(current)}</strong><span> / {targetText}{unit}</span></div>
    <progress aria-label={label} aria-valuetext={current === null ? '미확인' : undefined} max={100}
      value={current === null ? undefined : Math.min(100, numeric(metric?.progress_pct) ?? 0)}/>
    {status || hint ? <span className="competition-note">{status || hint}</span> : null}
  </div>;
}
export function CompetitionProgressView({ data, loading, error, onRefresh }: {
  data: CompetitionProgressData | null; loading: boolean; error: string | null; onRefresh: () => void;
}) {
  const kis = data?.kis, tf = data?.timefolio, turnover = tf?.turnover;
  const weekValue = numeric(turnover?.current_pct);
  const weekly: ProgressMetric = { value: weekValue, target: 5, met: turnover?.status === 'met',
    progress_pct: weekValue === null ? null : weekValue / 5 * 100 };
  const kisError = kis?.error || (kis?.stale ? '갱신 지연 · 이전 데이터' : null);
  const tfError = tf?.error || (tf?.stale && tf.updated_at ? '갱신 지연 · 이전 데이터' : null);
  return <section className="competition-progress" aria-labelledby="competition-title" aria-busy={loading && !data}>
    <div className="panel-heading"><h2 id="competition-title">대회 요건</h2>
      <button className="refresh-button" type="button" disabled={loading} onClick={onRefresh} aria-label="대회 요건 새로고침">{loading ? '조회 중' : '새로고침'}</button></div>
    {error ? <p className="history-error" role="alert">{error}{data ? ' · 이전 데이터' : ''}</p> : null}
    <div className="competition-groups">
      <div className="competition-kis">
        <div className="competition-source"><a href="https://vts3.koreainvestment.com/vts/#/univ/rule/1" target="_blank" rel="noreferrer">한투 AI리그</a>
          <span>{kis ? `${shortDate(kis.period.start_date)}–${shortDate(kis.period.end_date)} · 초기화 미반영` : '10.12–11.20'}</span>
          {kis?.phase === 'not_started' ? <span>시작 전</span> : kis?.phase === 'ended' ? <span>종료</span> : null}
        </div>
        <div className="competition-kis-metrics">
          <Meter label="거래일" unit="일" metric={kis?.metrics.trading_days ?? { value: null, target: 5, met: null, progress_pct: null }}/>
          <Meter label="누적 체결액" unit="원" metric={kis?.metrics.gross_amount ?? { value: null, target: 500_000_000, met: null, progress_pct: null }}/>
          <Meter label="거래 종목" unit="종목" metric={kis?.metrics.symbols ?? { value: null, target: 5, met: null, progress_pct: null }}/>
          <Meter label="API 체결" unit="회" metric={kis?.metrics.api_orders ?? { value: null, target: 5, met: null, progress_pct: null }} hint="확인된 주문 기준"/>
        </div>
        {kisError ? <p className="competition-error" role="status">{kisError}</p> : null}
        {kis?.updated_at ? <time className="competition-updated" dateTime={kis.updated_at}>{timestamp(kis.updated_at).text}</time> : null}
      </div>
      <div className="competition-timefolio">
        <div className="competition-source"><a href="https://contest.timefolio.net/" target="_blank" rel="noreferrer">타임폴리오</a>
          <span>{shortDate(turnover?.week_start)}–{shortDate(turnover?.week_end)}</span></div>
        <Meter label="주간 회전율" unit="%" metric={weekly}
          status={turnover?.status === 'exempt' ? '면제 주간' : turnover?.status === 'unknown' ? '공식 집계 대기'
            : turnover?.status === 'below' ? '목표 미달' : undefined}/>
        <span className={`competition-note${(turnover?.missed_weeks ?? 0) > 3 ? ' competition-error' : ''}`}>확정 미달 {number(turnover?.missed_weeks)}회 / 허용 3회</span>
        {tfError ? <p className="competition-error" role="status">{tfError}</p> : null}
        {tf?.updated_at ? <time className="competition-updated" dateTime={tf.updated_at}>{timestamp(tf.updated_at).text}</time> : null}
      </div>
    </div>
  </section>;
}
export default function CompetitionProgress() {
  const progress = useCompetitionProgress();
  return <CompetitionProgressView {...progress} onRefresh={() => { void progress.refresh(); }}/>;
}
