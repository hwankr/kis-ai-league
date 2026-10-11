import { timestamp } from '../format';
import { mirrorStates } from '../operations';
import type { OperationCommand, OperationsData } from '../operations';
import useOperations from '../useOperations';

interface Props {
  data: OperationsData | null; loading: boolean; pending: OperationCommand['action'] | null; error: string | null;
  onRefresh: () => void; onExecute: (command: OperationCommand) => Promise<boolean>;
}

export function OperationsStatus({ data, loading, pending, error, onRefresh, onExecute }: Props) {
  const locked = !data || !!pending || data.busy || !!error || data.mirror.status === 'unverified';
  const warning = error || data?.error;
  const dashboardTime = timestamp(data?.dashboard.updated_at ?? null);
  const mirrorTime = timestamp(data?.mirror.updated_at ?? null);
  const unknown = error || !data ? loading ? '조회 중' : '확인 필요' : null;
  return <section className="operations-panel" aria-labelledby="operations-title" aria-busy={loading && !data}>
    <div className="panel-heading lab-heading"><div className="panel-title"><h2 id="operations-title">실행 상태</h2>
      {data?.busy ? <span className="lab-badge" role="status">실행 요청 처리 중</span> : null}</div>
      <button type="button" className="lab-button" disabled={loading || !!pending} onClick={onRefresh}>실행 상태 새로고침</button></div>
    <dl className="operations-services">
      <div><dt>대시보드</dt><dd><span className="lab-badge" role="status">{unknown ?? (data?.dashboard.status === 'ok' ? '정상' : '응답 지연')}</span>
        <time dateTime={dashboardTime.dateTime}>{dashboardTime.text}</time></dd></div>
      <div><dt>타임폴리오</dt><dd><span className="lab-badge" role="status">{unknown ?? (data ? mirrorStates[data.mirror.status] : '확인 필요')}</span>
        <time dateTime={mirrorTime.dateTime}>{mirrorTime.text}</time></dd></div>
    </dl>
    {warning ? <p className="lab-error" role="alert">{warning}</p> : null}
    {data?.mirror.reason && data.mirror.reason !== warning ? <p className="lab-pause-reason">{data.mirror.reason}</p> : null}
    <div className="lab-actions operations-actions">
      <button type="button" className="lab-button" disabled={locked || !data?.mirror.can_start}
        onClick={() => { void onExecute({ action: 'mirror_start' }); }}>{pending === 'mirror_start' || data?.mirror.status === 'starting' ? '연동 시작 중' : '타임폴리오 연동 시작'}</button>
      <button type="button" className="lab-button lab-pause" disabled={locked || !data?.mirror.can_stop}
        onClick={() => { void onExecute({ action: 'mirror_stop' }); }}>{pending === 'mirror_stop' || data?.mirror.status === 'stopping' ? '연동 정지 중' : '타임폴리오 연동 정지'}</button>
    </div>
  </section>;
}

export default function OperationsPanel() {
  const operations = useOperations();
  return <OperationsStatus {...operations} onRefresh={() => { void operations.refresh(); }} onExecute={operations.execute}/>;
}
