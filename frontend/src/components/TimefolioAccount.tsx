import { number, numeric, percent, quantity, signClass, timestamp } from '../format';
import useTimefolioAccount from '../useTimefolioAccount';
import type { TimefolioData, TimefolioOrder } from '../timefolio';
import '../timefolio.css';

interface Props {
  data: TimefolioData | null; loading: boolean; error: string | null; autoRefresh: boolean;
  onAutoRefresh: (enabled: boolean) => void; onRefresh: () => void;
}
const orderStates: Record<string, string> = {
  completed: '완료', filled: '체결', cancelled: '취소', rejected: '거절', unknown: '미확정',
  working: '진행 중', pending: '대기', submitted: '접수', partial: '부분 체결', submitting: '접수 중',
};
const weight = (value: unknown) => numeric(value) === null ? '—' : `${number(value)}%`;

function OrderTime({ order }: { order: TimefolioOrder }) {
  const at = timestamp(order.created_at);
  return <time dateTime={at.dateTime}>{order.created_at ? at.text : order.order_time || '—'}</time>;
}

export function TimefolioAccountView({ data, loading, error, autoRefresh, onAutoRefresh, onRefresh }: Props) {
  const summary = data?.summary;
  const hasSnapshot = !!data?.account && !!summary && !!data.updated_at;
  const warning = error || data?.error;
  const stale = !!warning || data?.stale;
  const updated = timestamp(data?.updated_at ?? null);
  const status = hasSnapshot ? stale ? '갱신 지연 · 이전 데이터' : '조회 완료'
    : warning || data?.status === 'error' ? '확인 필요' : loading ? '조회 중' : '첫 조회 대기';
  const empty = hasSnapshot ? null : warning ? '계좌 현황 확인 필요' : loading ? '조회 중' : '첫 조회 대기';
  return <section className="timefolio-account" aria-labelledby="timefolio-account-title" aria-busy={loading && !hasSnapshot}>
    <div className="timefolio-toolbar">
      <div><h2 id="timefolio-account-title">계좌 현황</h2>
        {data?.account ? <p className="timefolio-contest">{data.account.contest}<span>기준일 {data.account.session_date}</span></p> : null}</div>
      <div className="refresh-controls">
        <label className="auto-refresh-control"><span>30초 자동 갱신</span>
          <input type="checkbox" role="switch" checked={autoRefresh} onChange={event => onAutoRefresh(event.target.checked)}/></label>
        <button className="refresh-button" type="button" disabled={loading} onClick={onRefresh} aria-busy={loading}>
          {loading ? '조회 중' : '새로고침'}</button>
      </div>
    </div>
    <div className="timefolio-status"><span className={`status-badge status-${stale ? 'stale' : hasSnapshot ? 'connected' : 'loading'}`} role="status">{status}</span>
      <span>마지막 조회 <time dateTime={updated.dateTime}>{updated.text}</time></span></div>
    {warning ? <p className="history-error timefolio-error" role="alert">{warning}</p> : null}
    <dl className="timefolio-summary">
      <div className="timefolio-nav"><dt>기준가 (NAV)</dt><dd>{number(summary?.nav)}</dd>
        <dd className={`timefolio-nav-change ${signClass(summary?.nav_change)}`}>{number(summary?.nav_change, true)} <span>{percent(summary?.nav_change_pct)}</span></dd></div>
      <div><dt>순위</dt><dd>{numeric(summary?.rank) === null ? '—' : <>{number(summary?.rank)}<span className="timefolio-unit">위</span></>}
        {numeric(summary?.participants) !== null ? <span className="timefolio-rank-total"> / {number(summary?.participants)}명</span> : null}</dd></div>
      <div><dt>보유 평가금액</dt><dd>{number(summary?.holdings_value)}<span className="timefolio-unit">원</span></dd></div>
      <div><dt>보유 평가손익</dt><dd className={signClass(summary?.holdings_pnl)}>{number(summary?.holdings_pnl, true)}<span className="timefolio-unit">원</span></dd>
        <dd className={`timefolio-summary-return ${signClass(summary?.holdings_return_pct)}`}>{percent(summary?.holdings_return_pct)}</dd></div>
    </dl>
    <section className="timefolio-table-panel" aria-labelledby="timefolio-holdings-title">
      <div className="panel-heading"><div className="panel-title"><h2 id="timefolio-holdings-title">보유 종목</h2>
        <span className="holdings-count">{hasSnapshot ? data.holdings.length : '—'}</span></div><span className="table-unit">원</span></div>
      {data?.holdings.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="타임폴리오 보유 종목 상세"><table className="timefolio-holdings-table">
        <thead><tr><th scope="col">종목</th><th scope="col">보유 수량</th><th scope="col">현재가</th><th scope="col">평균 매입가</th><th scope="col">평가금액</th><th scope="col">평가손익 / 수익률</th><th scope="col">비중</th></tr></thead>
        <tbody>{data.holdings.map(holding => <tr key={holding.symbol}>
          <td><span className="stock-name">{holding.name || '종목명 없음'}</span><span className="stock-symbol">{holding.symbol}</span></td>
          <td>{quantity(holding.quantity)}</td><td>{number(holding.price)}</td><td>{number(holding.avg_price)}</td><td>{number(holding.market_value)}</td>
          <td><span className={`cell-primary ${signClass(holding.pnl)}`}>{number(holding.pnl, true)}</span><span className={`cell-secondary ${signClass(holding.return_pct)}`}>{percent(holding.return_pct)}</span></td>
          <td>{weight(holding.weight_pct)}</td></tr>)}</tbody></table></div>
        : <p className="timefolio-empty">{empty ?? '보유 종목 없음'}</p>}
    </section>
    <section className="timefolio-table-panel" aria-labelledby="timefolio-orders-title">
      <div className="panel-heading"><div className="panel-title"><h2 id="timefolio-orders-title">주문 내역</h2>
        <span className="holdings-count">{hasSnapshot ? data.orders.length : '—'}</span></div></div>
      {data?.orders.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="타임폴리오 주문 상세"><table className="timefolio-orders-table">
        <thead><tr><th scope="col">일시</th><th scope="col">종목</th><th scope="col">구분</th><th scope="col">주문 비중</th><th scope="col">누적 체결수량</th><th scope="col">상태</th><th scope="col">연동</th></tr></thead>
        <tbody>{data.orders.map(order => <tr key={order.order_id}>
          <td className="timefolio-order-time"><OrderTime order={order}/></td><td><span className="stock-name">{order.name || '종목명 없음'}</span><span className="stock-symbol">{order.symbol}</span></td>
          <td className={order.side === 'buy' ? 'gain' : 'loss'}>{order.side === 'buy' ? '매수' : '매도'}</td><td>{weight(order.weight)}</td>
          <td><span className="cell-primary">{quantity(order.filled_quantity)}</span>{numeric(order.average_price) !== null ? <span className="cell-secondary">평균 {number(order.average_price)}원</span> : null}</td>
          <td><span className="timefolio-order-status">{order.verified ? orderStates[order.status] ?? '확인 필요' : '미확정'}</span>
            <span className="cell-secondary timefolio-order-id">{order.order_id}</span></td>
          <td>{order.system_owned ? '자동 연동' : '기타'}</td></tr>)}</tbody></table></div>
        : <p className="timefolio-empty">{empty ?? '주문 내역 없음'}</p>}
    </section>
  </section>;
}

export default function TimefolioAccount() {
  const account = useTimefolioAccount();
  return <TimefolioAccountView {...account} onAutoRefresh={account.setAutoRefresh} onRefresh={() => { void account.refresh(); }}/>;
}
