import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { numeric } from './format';
import useAccountCatalog from './useAccountCatalog';
import useTradeHistory from './useTradeHistory';
import type { AccountChoice, AccountSnapshot, HistoryData, HistoryPlaceholder, Holding, Numeric, Summary } from './types';

const REFRESH_MS = 30_000;
const CONNECTION_ERROR = '계좌 조회 실패 · 서버 연결 확인 필요';
const clearedAccount = {
  snapshot: null,
  history: null,
  historyPlaceholder: { state: 'loading', message: '조회 중' } as HistoryPlaceholder,
  error: null,
  status: 'loading',
  statusLabel: '계좌 연결 중',
  announcement: '',
  emptyTitle: '조회 중',
} as const;

interface DashboardState {
  loading: boolean;
  snapshot: AccountSnapshot | null;
  history: HistoryData | null;
  historyPlaceholder: HistoryPlaceholder;
  error: string | null;
  status: 'loading' | 'connected' | 'stale';
  statusLabel: string;
  announcement: string;
  emptyTitle: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function message(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

function readNumber(value: unknown): Numeric {
  return numeric(value) === null ? null : value as string | number;
}

function readHistory(value: unknown): HistoryData | null {
  const history = record(value);
  if (!history || !Array.isArray(history.points)) return null;
  return {
    points: history.points.flatMap((value: unknown) => {
      const point = record(value);
      if (!point || typeof point.observed_at !== 'string' || !Number.isFinite(Date.parse(point.observed_at))) return [];
      return [{ observed_at: point.observed_at, total_value: readNumber(point.total_value), cash: readNumber(point.cash) }];
    }),
    total_count: Math.max(0, numeric(history.total_count) ?? history.points.length),
    error: typeof history.error === 'string' ? history.error : null,
  };
}

function readSnapshot(data: Record<string, unknown>, account: AccountChoice, history: HistoryData | null): AccountSnapshot | null {
  if (typeof data.updated_at !== 'string' || !Number.isFinite(Date.parse(data.updated_at))) return null;
  const rawSummary = record(data.summary);
  if (!rawSummary || !Array.isArray(data.holdings)) throw new Error('Invalid account snapshot');
  const summary: Summary = {
    total_value: readNumber(rawSummary.total_value), cash: readNumber(rawSummary.cash),
    securities_value: readNumber(rawSummary.securities_value), purchase_amount: readNumber(rawSummary.purchase_amount),
    unrealized_pnl: readNumber(rawSummary.unrealized_pnl), unrealized_return_pct: readNumber(rawSummary.unrealized_return_pct),
  };
  const holdings: Holding[] = data.holdings.map((value: unknown) => {
    const holding = record(value);
    if (!holding || typeof holding.symbol !== 'string' || typeof holding.name !== 'string') {
      throw new Error('Invalid holding');
    }
    return {
      symbol: holding.symbol, name: holding.name, quantity: readNumber(holding.quantity),
      avg_price: readNumber(holding.avg_price), price: readNumber(holding.price), market_value: readNumber(holding.market_value),
      purchase_amount: readNumber(holding.purchase_amount), pnl: readNumber(holding.pnl), return_pct: readNumber(holding.return_pct),
    };
  });
  return {
    status: data.status === 'ok' ? 'ok' : 'error', environment: typeof data.environment === 'string' ? data.environment : 'paper',
    account: { id: account.id, name: account.name }, updated_at: data.updated_at,
    refresh_interval_seconds: numeric(data.refresh_interval_seconds) ?? 30, stale: data.stale === true,
    error: typeof data.error === 'string' ? data.error : null, summary, holdings,
    history: history ?? { points: [], total_count: 0, error: '이력 조회 실패' },
  };
}

export default function useAccountDashboard({ catalogEnabled = true, balanceEnabled = true, tradesEnabled = true } = {}) {
  const catalog = useAccountCatalog(catalogEnabled);
  const selectedAccount = catalog.accounts.find(account => account.id === catalog.selectedId);
  const account = catalog.loaded && !catalog.error && selectedAccount?.configured ? selectedAccount : null;
  const accountId = account?.id ?? null;
  const trades = useTradeHistory(tradesEnabled);
  const { refresh: refreshTrades, clear: clearTrades, cancel: cancelTrades } = trades;
  const [state, setState] = useState<DashboardState>(() => ({ ...clearedAccount, loading: false }));
  const [autoRefresh, setAutoRefresh] = useState(true);
  const session = useRef({
    account, balanceEnabled, tradesEnabled, catalogLoading: catalog.loading, tradesLoading: trades.loading,
    resumePending: false, generation: 0, inFlight: false,
    controller: null as AbortController | null, timeout: undefined as number | undefined, mounted: false,
  });

  const cancelBalance = useCallback(() => {
    const current = session.current;
    ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
    current.inFlight = false;
    setState(previous => previous.loading ? { ...previous, loading: false } : previous);
  }, []);

  const fail = useCallback((error: string) => {
    setState(previous => ({
      ...previous,
      historyPlaceholder: previous.historyPlaceholder.state === 'loading'
        ? { state: 'error', message: '이력 조회 실패' } : previous.historyPlaceholder,
      error: previous.snapshot ? `${error} · 이전 데이터` : error,
      status: 'stale', statusLabel: previous.snapshot ? '갱신 지연 · 이전 데이터' : '연결 확인 필요',
      emptyTitle: previous.snapshot ? previous.emptyTitle : '계좌 조회 실패',
      announcement: '계좌 조회에 실패했습니다.',
    }));
  }, []);

  const refreshBalance = useCallback(async () => {
    const current = session.current;
    const account = current.account;
    if (!current.mounted || !current.balanceEnabled || !account || current.inFlight || document.hidden) return;
    const generation = ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    const controller = new AbortController();
    current.controller = controller;
    current.inFlight = true;
    setState(previous => ({ ...previous, loading: true }));
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    const isCurrent = () => current.mounted && current.balanceEnabled && !document.hidden
      && generation === current.generation && current.account?.id === account.id;
    try {
      const options: RequestInit = {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      };
      const requestAccountId = account.id;
      const response = await fetch(`/api/account?account=${encodeURIComponent(requestAccountId)}`, options);
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error('Request timed out');
      if (response.status === 404) {
        setState(previous => ({ ...previous, ...clearedAccount }));
        fail('선택 계좌 없음 · 새로고침 후 다시 선택');
        return;
      }
      const data = record(payload);
      if (!data || (data.status !== 'ok' && data.status !== 'error')) throw new Error('Invalid response');
      if (record(data.account)?.id !== requestAccountId) {
        // A config failure or mismatched identity cannot reuse a previous account's values.
        setState(previous => ({ ...previous, ...clearedAccount }));
        fail(message(data.error, CONNECTION_ERROR));
        return;
      }
      if (data.status === 'error' && !data.updated_at && !data.stale) {
        setState(previous => ({ ...previous, ...clearedAccount }));
      }
      const history = readHistory(data.history);
      const snapshot = readSnapshot(data, account, history);
      setState(previous => ({
        ...previous, history, historyPlaceholder: { state: 'error', message: '이력 조회 실패' },
        ...(snapshot ? { snapshot, emptyTitle: '보유 종목 없음' } : {}),
      }));
      if (!response.ok || data.status === 'error' || data.stale) {
        fail(message(data.error, '계좌 갱신 실패'));
      } else if (snapshot) {
        setState(previous => ({ ...previous, error: null, status: 'connected', statusLabel: '계좌 연결됨',
          announcement: '계좌 정보가 갱신되었습니다.' }));
      } else {
        throw new Error('Missing account snapshot');
      }
    } catch {
      if (!isCurrent()) return;
      fail(CONNECTION_ERROR);
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        current.inFlight = false;
        current.controller = null;
        current.timeout = undefined;
        setState(previous => ({ ...previous, loading: false }));
      }
    }
  }, [fail]);

  const refreshCurrent = useCallback(() => {
    const current = session.current;
    if (!current.mounted || !current.account || current.catalogLoading || !catalog.isReady() || document.hidden) return;
    current.resumePending = false;
    if (current.balanceEnabled) void refreshBalance();
    if (current.tradesEnabled) void refreshTrades(current.account.id);
  }, [refreshBalance, refreshTrades, catalog.isReady]);

  const refresh = useCallback(async ({ reloadAccounts = false } = {}) => {
    if (reloadAccounts) await catalog.refresh();
    else refreshCurrent();
  }, [catalog.refresh, refreshCurrent]);

  useLayoutEffect(() => {
    const current = session.current;
    current.mounted = true;
    return () => { current.mounted = false; cancelBalance(); };
  }, [cancelBalance]);

  useLayoutEffect(() => {
    const current = session.current;
    if ((current.account?.id ?? null) !== accountId) {
      cancelBalance();
      clearTrades();
      setState({ ...clearedAccount, loading: false });
    }
    current.account = account;
    current.catalogLoading = catalog.loading;
    current.tradesLoading = trades.loading;
    current.balanceEnabled = balanceEnabled;
    current.tradesEnabled = tradesEnabled;
    if (!balanceEnabled) cancelBalance();
  }, [accountId, account, catalog.loading, trades.loading, balanceEnabled, tradesEnabled, cancelBalance, clearTrades]);

  useEffect(() => {
    refreshCurrent();
    return () => {
      cancelBalance();
      cancelTrades();
    };
  }, [balanceEnabled, tradesEnabled, accountId, catalog.loading, catalog.revision, refreshCurrent, cancelBalance, cancelTrades]);

  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearInterval(timer);
      timer = autoRefresh && accountId && (balanceEnabled || tradesEnabled) && !document.hidden
        ? window.setInterval(refreshCurrent, REFRESH_MS) : undefined;
    };
    const onVisibility = () => {
      schedule();
      if (document.hidden) {
        session.current.resumePending = session.current.inFlight || session.current.tradesLoading;
        cancelBalance(); cancelTrades();
      } else if (autoRefresh || session.current.resumePending) refreshCurrent();
    };
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility); };
  }, [autoRefresh, balanceEnabled, tradesEnabled, accountId, refreshCurrent, cancelBalance, cancelTrades]);

  const selectionError = catalog.loaded && !account
    ? selectedAccount ? '계좌 설정 필요 · 설정 후 새로고침'
      : catalog.selectedId ? '선택 계좌 없음 · 계좌 다시 선택' : '계좌 선택 필요 · 미등록 시 계좌 설정'
    : null;
  const unavailable = catalog.error || selectionError;
  return { ...state, ...(unavailable ? { ...clearedAccount, loading: false, status: 'stale' as const,
    statusLabel: catalog.error ? '계좌 목록 확인 필요' : '계좌 선택 확인',
    emptyTitle: catalog.error ? '계좌 목록 확인 필요' : selectedAccount ? '계좌 설정 필요' : '계좌 선택 필요',
    historyPlaceholder: { state: 'error' as const, message: unavailable } } : {}),
    loading: balanceEnabled && (state.loading || catalog.loading),
    accounts: catalog.accounts, selectedId: catalog.selectedId, catalogLoaded: catalog.loaded,
    catalogLoading: catalog.loading, catalogError: catalog.error, selectionError,
    autoRefresh, setAutoRefresh, selectAccount: catalog.selectAccount, refresh, refreshCatalog: catalog.refresh, trades };
}
