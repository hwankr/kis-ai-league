import { useState, type FormEvent } from 'react';
import { number, quantity, timestamp } from '../format';
import { todayInSeoul, tradeRangeError } from '../useTradeHistory';
import type { TradeHistory as TradeData, TradeRange } from '../types';

interface Props {
  data: TradeData | null;
  range: TradeRange;
  accountId: string | null;
  loading: boolean;
  error: string | null;
  onQuery: (range: TradeRange) => void;
}

export default function TradeHistory({ data, range, accountId, loading, error, onQuery }: Props) {
  const [draft, setDraft] = useState(range);
  const [validation, setValidation] = useState<string | null>(null);
  const updated = timestamp(data?.updated_at ?? null);
  const hasTrades = Boolean(data?.trades.length);
  const hasResult = data?.status === 'ok' || Boolean(data?.updated_at) || hasTrades;
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const invalid = tradeRangeError(draft);
    setValidation(invalid);
    if (!invalid) onQuery(draft);
  };

  return <section id="trade-history" className="trades-panel" aria-labelledby="trades-title" aria-busy={loading}
    data-account-id={accountId ?? ''} data-start-date={range.start} data-end-date={range.end} data-row-count={data?.trades.length ?? 0}>
    <div className="panel-heading">
      <div className="panel-title"><h2 id="trades-title">거래 내역</h2><span id="trades-count" className="holdings-count">{hasResult ? data?.total_count : '—'}</span></div>
      <span className="table-unit">주문별 체결 · 원</span>
    </div>
    <form className="trade-filters" onSubmit={submit}>
      <label htmlFor="trade-start">시작일<input id="trade-start" type="date" required value={draft.start} max={todayInSeoul()}
        onChange={event => { setDraft(previous => ({ ...previous, start: event.target.value })); setValidation(null); }}/></label>
      <label htmlFor="trade-end">종료일<input id="trade-end" type="date" required value={draft.end} max={todayInSeoul()}
        onChange={event => { setDraft(previous => ({ ...previous, end: event.target.value })); setValidation(null); }}/></label>
      <button className="refresh-button trade-query-button" type="submit" disabled={!accountId}>조회</button>
    </form>
    <div className="trade-status" role="status" aria-live="polite">
      <span>{loading ? '거래 내역 조회 중' : error && hasResult ? '갱신 지연 · 이전 데이터' : ''}</span>
      {data?.updated_at ? <time dateTime={updated.dateTime}>조회 {updated.text}</time> : null}
    </div>
    {validation || error ? <p id="trades-error" className="history-error" role="alert">{validation || error}</p> : null}
    {hasTrades ? <div className="table-scroll trades-table-container" tabIndex={0} role="region" aria-label="거래 내역 상세">
      <table className="trades-table">
        <thead><tr><th scope="col">주문일</th><th scope="col">종목</th><th scope="col">구분</th><th scope="col">체결 수량</th><th scope="col">평균 체결가</th></tr></thead>
        <tbody>{data?.trades.map(trade => <tr key={`${trade.order_date}-${trade.branch_id}-${trade.order_id}`}>
          <td className="trade-date"><time dateTime={trade.order_date}>{trade.order_date.replaceAll('-', '.')}</time></td>
          <td className="trade-stock"><span className="stock-name">{trade.name || '종목명 없음'}</span><span className="stock-symbol">{trade.symbol}</span></td>
          <td className={`trade-side ${trade.side === 'buy' ? 'gain' : 'loss'}`}>{trade.side === 'buy' ? '매수' : '매도'}</td>
          <td className="trade-quantity">{quantity(trade.quantity)}</td>
          <td className="trade-price"><span className="trade-price-label">평균 체결가 </span>{number(trade.price)}<span className="trade-price-unit">원</span></td>
        </tr>)}</tbody>
      </table>
    </div> : !error ? <p id="trades-empty" className="trades-empty">{loading ? '거래 내역을 불러오는 중' : data?.status === 'ok' ? '조회 기간에 체결 내역이 없습니다' : '조회할 계좌를 확인해 주세요'}</p> : null}
  </section>;
}
