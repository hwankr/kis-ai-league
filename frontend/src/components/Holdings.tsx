import { number, percent, quantity, signClass } from '../format';
import type { Holding } from '../types';

interface Props { holdings: Holding[] | null; emptyTitle: string }

export default function Holdings({ holdings, emptyTitle }: Props) {
  const hasHoldings = Boolean(holdings?.length);
  return <section className="holdings-panel" aria-labelledby="holdings-title">
    <div className="panel-heading">
      <div className="panel-title"><h2 id="holdings-title">보유 종목</h2><span id="holdings-count" className="holdings-count">{holdings ? holdings.length : '—'}</span></div>
      <span className="table-unit">단위: 원</span>
    </div>
    <div id="holdings-table-container" className="table-scroll" tabIndex={0} role="region" aria-label="보유 종목 상세" hidden={!hasHoldings}>
      <table>
        <thead><tr><th scope="col">종목</th><th scope="col">보유 수량</th><th scope="col">평균 매입가</th><th scope="col">현재가</th><th scope="col">평가금액</th><th scope="col">평가손익 / 수익률</th></tr></thead>
        <tbody id="holdings-body">{holdings?.map((holding, index) => <tr key={`${holding.symbol}-${index}`}>
          <td><span className="stock-name">{holding.name || '종목명 없음'}</span><span className="stock-symbol">{holding.symbol || '—'}</span></td>
          <td className="holding-quantity">{quantity(holding.quantity)}</td>
          <td className="holding-average">{number(holding.avg_price)}</td>
          <td className="holding-price">{number(holding.price)}</td>
          <td className="holding-value">{number(holding.market_value)}</td>
          <td className="holding-pnl"><span className={`cell-primary ${signClass(holding.pnl)}`}>{number(holding.pnl, true)}</span><span className={`cell-secondary ${signClass(holding.return_pct)}`}>{percent(holding.return_pct)}</span></td>
        </tr>)}</tbody>
      </table>
    </div>
    <div id="holdings-empty" className="empty-state" hidden={hasHoldings}>
      <svg className="empty-illustration" viewBox="0 0 88 80" fill="none" aria-hidden="true"><rect x="23" y="9" width="43" height="53" rx="8" fill="#e8edf4" transform="rotate(10 23 9)"/><rect x="18" y="17" width="44" height="54" rx="8" fill="#f2f5fa"/><path d="M30 55V46M40 55V34M50 55V40" stroke="#8db9f5" strokeWidth="6" strokeLinecap="round"/><path d="M29 28h12" stroke="#c4cfdd" strokeWidth="3" strokeLinecap="round"/></svg>
      <h3 id="empty-title">{emptyTitle}</h3>
    </div>
  </section>;
}
