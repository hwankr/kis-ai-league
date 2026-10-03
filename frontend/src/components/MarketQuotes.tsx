import { number, percent, signClass, timestamp } from '../format';
import type { MarketData } from '../types';

interface Props { data: MarketData | null; loading: boolean; error: string | null }

const labels = { running: '수집 중', stopped: '수집 중지', stale: '수집 지연', not_started: '수집 전' };

export default function MarketQuotes({ data, loading, error }: Props) {
  const state = data?.collector.state;
  const status = error ? '상태 확인 필요' : state ? labels[state] : loading ? '상태 조회 중' : '상태 확인 필요';
  const warning = error || data?.collector.error;
  const hasRows = Boolean(data?.quotes.length);
  return <section id="market-quotes" className="market-panel" aria-labelledby="market-title" aria-busy={loading}
    data-row-count={data?.quotes.length ?? 0}>
    <div className="panel-heading">
      <div className="panel-title"><h2 id="market-title">시세 수집</h2></div>
      <span id="collector-status" className={`status-badge ${error || state === 'stale' ? 'status-stale' : state === 'running' ? 'status-connected' : 'status-loading'}`} role="status">
        <span className="status-dot" aria-hidden="true"></span>{status}
      </span>
    </div>
    {data ? <p className="market-meta"><span>KRX</span><span>{number(data.collector.interval_seconds)}초 간격</span></p> : null}
    {warning ? <p id="market-error" className="history-error" role="alert">{warning}</p> : null}
    {hasRows ? <div className="table-scroll market-table-container" tabIndex={0} role="region" aria-label="수집 시세 상세">
      <table className="market-table">
        <thead><tr><th scope="col">종목</th><th scope="col">최근 조회가</th><th scope="col">등락률</th><th scope="col">누적 거래량</th><th scope="col">조회 시각</th><th scope="col">저장 기록</th></tr></thead>
        <tbody>{data?.quotes.map(quote => {
          const observed = timestamp(quote.observed_at);
          return <tr key={quote.symbol} data-symbol={quote.symbol}>
            <td className="market-stock"><span className="stock-name">{quote.name || quote.symbol}</span>{quote.name ? <span className="stock-symbol">{quote.symbol}</span> : null}
              {quote.error ? <span className="market-row-error" role="status">{quote.observed_at ? '갱신 실패 · 이전 시세' : '수집 실패'}<span>{quote.error}</span></span>
                : !quote.observed_at ? <span className="stock-symbol">저장된 시세 없음</span> : null}</td>
            <td className="market-price">{number(quote.price)}<span className="market-price-unit">원</span></td>
            <td className={`market-change ${signClass(quote.change_percent)}`}>{percent(quote.change_percent)}</td>
            <td className="market-volume"><span className="market-mobile-label">누적 거래량 </span>{number(quote.volume)}주</td>
            <td className="market-time"><span className="market-mobile-label">조회 </span><time dateTime={observed.dateTime}>{observed.text}</time></td>
            <td className="market-count">{number(quote.total_count)}건</td>
          </tr>;
        })}</tbody>
      </table>
    </div> : !warning ? <p className="market-empty">{loading ? '시세 수집 상태를 불러오는 중' : '등록된 관심 종목이 없습니다'}</p> : null}
  </section>;
}
