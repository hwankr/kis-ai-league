import AccountPicker from './components/AccountPicker';
import HistoryChart from './components/HistoryChart';
import Holdings from './components/Holdings';
import TradeHistory from './components/TradeHistory';
import MarketQuotes from './components/MarketQuotes';
import { number, percent, signClass, timestamp } from './format';
import useAccountDashboard from './useAccountDashboard';
import useMarketData from './useMarketData';

export default function App() {
  const dashboard = useAccountDashboard();
  const market = useMarketData(dashboard.autoRefresh);
  const account = dashboard.accounts.find(account => account.id === dashboard.selectedId);
  const summary = dashboard.snapshot?.summary;
  const updated = timestamp(dashboard.snapshot?.updated_at ?? null);

  return <>
    <a className="skip-link" href="#main-content">본문으로 바로가기</a>
    <header className="app-header">
      <div className="header-inner">
        <a className="brand" href="/" aria-label="KIS AI League 홈">
          <svg className="brand-mark" viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M5 24V14M16 24V6M27 24V11" stroke="currentColor" strokeWidth="6" strokeLinecap="round"/></svg>
          <span>KIS <span className="brand-light">AI League</span></span>
        </a>
        <nav className="header-nav" aria-label="메인 메뉴"><a href="#main-content" aria-current="page">내 계좌</a></nav>
        <div className="header-meta"><span className="environment-badge">모의투자</span><span className="read-only">조회 전용</span></div>
      </div>
    </header>

    <main id="main-content">
      <div className="page-heading">
        <h1 id="page-title">내 계좌</h1>
        <div className="refresh-controls">
          <label className="auto-refresh-control">
            <span>30초 자동 갱신</span>
            <input id="auto-refresh" type="checkbox" role="switch" checked={dashboard.autoRefresh} onChange={event => dashboard.setAutoRefresh(event.target.checked)}/>
          </label>
          <button id="refresh-button" className="refresh-button" type="button" disabled={dashboard.loading} aria-busy={dashboard.loading} onClick={() => { void dashboard.refresh({ reloadAccounts: true }); void market.refresh(); }}>
            <svg className="refresh-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M16.4 8.2A6.5 6.5 0 1 0 16 13M16.4 3.8v4.4H12" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg>
            <span id="refresh-label">{dashboard.loading ? '조회 중' : '새로고침'}</span>
          </button>
        </div>
      </div>
      <div id="error-notice" className="error-notice" role="alert" hidden={!dashboard.error}>{dashboard.error}</div>
      <p id="refresh-announcement" className="sr-only" role="status" aria-live="polite">{dashboard.announcement}</p>

      <div className="dashboard-layout" id="account-summary" aria-busy={dashboard.loading}>
        <section className="asset-overview" aria-labelledby="asset-title">
          <AccountPicker accounts={dashboard.accounts} selectedId={dashboard.selectedId} loading={!dashboard.catalogLoaded && dashboard.loading}
            emptyLabel={!dashboard.catalogLoaded && !dashboard.loading ? '계좌 목록 확인 필요' : undefined} onSelect={dashboard.selectAccount}/>
          <h2 id="asset-title">총 평가금액</h2>
          <p className="total-value"><span id="total-value">{number(summary?.total_value)}</span><span className="total-currency">원</span></p>
          <div className="pnl-summary">
            <span className="pnl-label">보유 종목 평가손익</span>
            <span className="pnl-amount"><span id="unrealized-pnl" className={signClass(summary?.unrealized_pnl)}>{number(summary?.unrealized_pnl, true)}</span>원</span>
            <span id="unrealized-return" className={`return-value ${signClass(summary?.unrealized_return_pct)}`}>{percent(summary?.unrealized_return_pct)}</span>
          </div>
        </section>

        <aside className="account-details" aria-labelledby="details-title">
          <h2 id="details-title">자산 상세</h2>
          <dl className="balance-list">
            <div><dt><span className="asset-dot cash-dot" aria-hidden="true"></span>예수금</dt><dd><span id="cash">{number(summary?.cash)}</span><span className="unit">원</span></dd></div>
            <div><dt><span className="asset-dot stock-dot" aria-hidden="true"></span>주식 평가금액</dt><dd><span id="securities-value">{number(summary?.securities_value)}</span><span className="unit">원</span></dd></div>
          </dl>
        </aside>
        <aside className="connection-panel" aria-labelledby="connection-title">
          <div className="account-connection">
            <h2 id="connection-title">연결 상태</h2>
            <span id="connection-status" className={`status-badge status-${dashboard.status}`}><span className="status-dot" aria-hidden="true"></span><span id="connection-label">{dashboard.statusLabel}</span></span>
            <dl className="connection-details">
              <div><dt>마지막 조회</dt><dd><time id="updated-at" dateTime={updated.dateTime}>{updated.text}</time></dd></div>
              <div><dt>조회 범위</dt><dd>국내주식</dd></div>
            </dl>
          </div>
        </aside>

        <HistoryChart history={dashboard.history} accountId={dashboard.selectedId} accountName={account?.name ?? '선택 계좌'} placeholder={dashboard.historyPlaceholder}/>
        <Holdings holdings={dashboard.snapshot?.holdings ?? null} emptyTitle={dashboard.emptyTitle}/>
        <TradeHistory data={dashboard.trades.data} range={dashboard.trades.range} accountId={dashboard.trades.accountId}
          loading={dashboard.trades.loading} error={dashboard.trades.error} onQuery={dashboard.trades.query}/>
        <MarketQuotes data={market.data} loading={market.loading} error={market.error}/>
      </div>
      <footer className="page-footer"><span>KIS AI League</span><span id="account-label">{account?.name ?? '계좌 선택 필요'}</span></footer>
    </main>
  </>;
}
