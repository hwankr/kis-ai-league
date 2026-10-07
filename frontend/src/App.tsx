import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import AccountPicker from './components/AccountPicker';
import HistoryChart from './components/HistoryChart';
import Holdings from './components/Holdings';
import TradeHistory from './components/TradeHistory';
import MarketQuotes from './components/MarketQuotes';
import StockChart from './components/StockChart';
import CandidateComparison from './components/CandidateComparison';
import ResearchObservation from './components/ResearchObservation';
import ExperimentLab from './components/ExperimentLab';
import { number, percent, signClass, timestamp } from './format';
import useAccountDashboard from './useAccountDashboard';
import useMarketData from './useMarketData';
import useStockChart from './useStockChart';
import useCandidateComparison from './useCandidateComparison';
import usePageNavigation, { chartHref, navigate, pages } from './usePageNavigation';
import type { ChartInterval } from './types';

export default function App() {
  const dashboard = useAccountDashboard();
  const market = useMarketData(dashboard.autoRefresh);
  const chart = useStockChart();
  const candidates = useCandidateComparison();
  const route = usePageNavigation();
  const content = useRef<HTMLElement>(null);
  const previousPage = useRef(route.page);
  const accountPage = route.page === 'account' || route.page === 'trades';
  const pageLoading = route.page === 'chart' ? market.loading : dashboard.loading;
  const account = dashboard.accounts.find(account => account.id === dashboard.selectedId);
  const summary = dashboard.snapshot?.summary;
  const updated = timestamp(dashboard.snapshot?.updated_at ?? null);

  useEffect(() => {
    if (route.request) void chart.query(route.request.symbol, route.request.interval);
  }, [route.page, route.request?.symbol, route.request?.interval, chart.query]);

  useEffect(() => {
    if (route.page === 'candidates') void candidates.refresh();
  }, [route.page, candidates.refresh]);

  useLayoutEffect(() => {
    const changedPage = previousPage.current !== route.page;
    previousPage.current = route.page;
    document.title = `${route.title} · KIS AI League`;
    document.getElementById('page-title')?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    if (!changedPage || !content.current?.animate
      || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const animation = content.current.animate([{ opacity: 0.35 }, { opacity: 1 }], {
      duration: 180, easing: 'cubic-bezier(0.2, 0, 0, 1)',
    });
    return () => animation.cancel();
  }, [route.page, route.title]);

  const queryChart = useCallback((symbol: string, interval: ChartInterval) => {
    const normalized = symbol.trim().toUpperCase();
    if (!/^[0-9A-Z]{6}$/.test(normalized)) return Promise.resolve();
    const href = chartHref({ symbol: normalized, interval });
    if (window.location.hash === href) return chart.query(normalized, interval);
    navigate(href);
    return Promise.resolve();
  }, [chart.query]);
  const selectSymbol = (symbol: string) => {
    if (route.page === 'chart') {
      document.getElementById('page-title')?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    }
    void queryChart(symbol, chart.interval);
  };

  return <>
    <a className="skip-link" href="#main-content" onClick={event => {
      event.preventDefault();
      document.getElementById('main-content')?.focus();
      document.getElementById('main-content')?.scrollIntoView({ block: 'start' });
    }}>본문으로 바로가기</a>
    <header className="app-header">
      <div className="header-inner">
        <a className="brand" href="#/account" aria-label="KIS AI League 홈">
          <svg className="brand-mark" viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M5 24V14M16 24V6M27 24V11" stroke="currentColor" strokeWidth="6" strokeLinecap="round"/></svg>
          <span>KIS <span className="brand-light">AI League</span></span>
        </a>
        <nav className="header-nav" aria-label="메인 메뉴">{pages.map(page => <a key={page.id}
          href={page.id === 'chart' && chart.request ? chartHref(chart.request) : `#/${page.id}`}
          aria-current={route.page === page.id ? 'page' : undefined}>{page.label}</a>)}</nav>
        <div className="header-meta"><span className="environment-badge">모의투자</span></div>
      </div>
    </header>

    <main id="main-content" ref={content} tabIndex={-1}>
      <div className="page-heading">
        <h1 id="page-title" tabIndex={-1}>{route.title}</h1>
        <div className="refresh-controls" hidden={route.page === 'experiments'} inert={route.page === 'candidates' || route.page === 'experiments'} aria-hidden={route.page === 'candidates' || route.page === 'experiments'}
          style={{ visibility: route.page === 'candidates' || route.page === 'experiments' ? 'hidden' : undefined }}>
          <label className="auto-refresh-control">
            <span>30초 자동 갱신</span>
            <input id="auto-refresh" type="checkbox" role="switch" checked={dashboard.autoRefresh} onChange={event => dashboard.setAutoRefresh(event.target.checked)}/>
          </label>
          <button id="refresh-button" className="refresh-button" type="button" disabled={pageLoading} aria-busy={pageLoading}
            onClick={() => { if (route.page === 'chart') void market.refresh(); else void dashboard.refresh({ reloadAccounts: true }); }}>
            <svg className="refresh-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M16.4 8.2A6.5 6.5 0 1 0 16 13M16.4 3.8v4.4H12" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg>
            <span id="refresh-label">{pageLoading ? '조회 중' : route.page === 'chart' ? '시세 새로고침' : '새로고침'}</span>
          </button>
        </div>
      </div>
      <div id="error-notice" className="error-notice" role="alert" hidden={!accountPage || !dashboard.error}>{dashboard.error}</div>
      <p id="refresh-announcement" className="sr-only" role="status" aria-live="polite">{accountPage ? dashboard.announcement : ''}</p>
      {accountPage ? <div className="page-account-picker">
        <AccountPicker accounts={dashboard.accounts} selectedId={dashboard.selectedId} loading={!dashboard.catalogLoaded && dashboard.loading}
          emptyLabel={!dashboard.catalogLoaded && !dashboard.loading ? '계좌 목록 확인 필요' : undefined} onSelect={dashboard.selectAccount}/>
      </div> : null}

      <div className="app-page" hidden={route.page !== 'account'}>
      <div className="dashboard-layout" id="account-summary" aria-busy={dashboard.loading}>
        <section className="asset-overview" aria-labelledby="asset-title">
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
      </div>
      </div>
      <div className="app-page page-content" hidden={route.page !== 'trades'}>
        <TradeHistory data={dashboard.trades.data} range={dashboard.trades.range} accountId={dashboard.trades.accountId}
          loading={dashboard.trades.loading} error={dashboard.trades.error} onQuery={dashboard.trades.query}/>
      </div>
      <div className="app-page page-content" hidden={route.page !== 'candidates'}>
        {route.page === 'candidates' ? <ResearchObservation onSelectSymbol={symbol => { void queryChart(symbol, 'day'); }}/> : null}
        <CandidateComparison data={candidates.data} loading={candidates.loading} error={candidates.error}
          onStart={() => { void candidates.start(); }} onRefresh={() => { void candidates.refresh(); }} onSelectSymbol={selectSymbol}/>
      </div>
      <div className="app-page page-content" hidden={route.page !== 'chart'}>
        {route.invalidChart ? <p role="alert" className="history-error">차트 주소의 종목코드 또는 주기가 올바르지 않습니다.</p> : null}
        <StockChart chart={{ ...chart, query: queryChart }}/>
        <MarketQuotes data={market.data} loading={market.loading} error={market.error} onSelectSymbol={selectSymbol}/>
      </div>
      <div className="app-page page-content" hidden={route.page !== 'experiments'}>
        {route.page === 'experiments' ? <ExperimentLab accounts={dashboard.accounts} onSelectSymbol={symbol => { void queryChart(symbol, 'day'); }}/> : null}
      </div>
      <footer className="page-footer"><span>KIS AI League</span>{accountPage ? <span id="account-label">{account?.name ?? '계좌 선택 필요'}</span> : null}</footer>
    </main>
  </>;
}
