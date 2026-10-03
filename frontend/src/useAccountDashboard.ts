import { useCallback, useEffect, useRef, useState } from 'react';
import { numeric } from './format';
import useTradeHistory from './useTradeHistory';
import type { AccountChoice, AccountSnapshot, HistoryData, HistoryPlaceholder, Holding, Numeric, Summary } from './types';

const ACCOUNT_STORAGE_KEY = 'kis-dashboard-account';
const REFRESH_MS = 30_000;
const CONNECTION_ERROR = '계좌를 불러오지 못했습니다. 서버 연결 상태를 확인한 뒤 다시 조회해 주세요.';
const clearedAccount = {
  snapshot: null,
  history: null,
  historyPlaceholder: { state: 'loading', message: '이력을 불러오는 중' } as HistoryPlaceholder,
  error: null,
  status: 'loading',
  statusLabel: '계좌 연결 중',
  announcement: '',
  emptyTitle: '계좌를 불러오고 있어요',
} as const;

interface DashboardState {
  accounts: AccountChoice[];
  selectedId: string | null;
  catalogLoaded: boolean;
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

function savedSelection(): string | null {
  try { return localStorage.getItem(ACCOUNT_STORAGE_KEY) || null; }
  catch { return null; }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function message(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

function readCatalog(value: unknown): { accounts: AccountChoice[]; defaultId: string | null } {
  const catalog = record(value);
  if (!catalog || !Array.isArray(catalog.accounts)) throw new Error('Invalid account catalog');
  const ids = new Set<string>();
  const accounts = catalog.accounts.map((value: unknown): AccountChoice => {
    const account = record(value);
    if (!account || typeof account.id !== 'string' || !account.id || ids.has(account.id)
      || typeof account.name !== 'string' || typeof account.configured !== 'boolean') {
      throw new Error('Invalid account catalog');
    }
    ids.add(account.id);
    return { id: account.id, name: account.name, configured: account.configured };
  });
  return { accounts, defaultId: typeof catalog.default_account === 'string' ? catalog.default_account : null };
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
    history: history ?? { points: [], total_count: 0, error: '이력을 불러오지 못했습니다' },
  };
}

export default function useAccountDashboard() {
  const trades = useTradeHistory();
  const { refresh: refreshTrades, clear: clearTrades } = trades;
  const [initialSelection] = useState(savedSelection);
  const [state, setState] = useState<DashboardState>(() => ({
    ...clearedAccount, accounts: [], selectedId: initialSelection, catalogLoaded: false, loading: true,
  }));
  const [autoRefresh, setAutoRefresh] = useState(true);
  const session = useRef({
    accounts: [] as AccountChoice[], selectedId: initialSelection, catalogLoaded: false,
    selectionInitialized: initialSelection !== null, generation: 0, inFlight: false,
    controller: null as AbortController | null, timeout: undefined as number | undefined, mounted: false,
  });

  const fail = useCallback((error: string) => {
    setState(previous => ({
      ...previous,
      historyPlaceholder: previous.historyPlaceholder.state === 'loading'
        ? { state: 'error', message: '이력을 불러오지 못했습니다' } : previous.historyPlaceholder,
      error: previous.snapshot ? `${error} 이전 조회 데이터를 표시하고 있습니다.` : error,
      status: 'stale', statusLabel: previous.snapshot ? '갱신 지연 · 이전 데이터' : '연결 확인 필요',
      emptyTitle: previous.snapshot ? previous.emptyTitle : '계좌 정보를 불러오지 못했습니다',
      announcement: '계좌 조회에 실패했습니다.',
    }));
  }, []);

  const refresh = useCallback(async ({ reloadAccounts = false, switchAccount = false } = {}) => {
    const current = session.current;
    if (!current.mounted || (current.inFlight && !switchAccount) || document.hidden) return;
    const generation = ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    const controller = new AbortController();
    current.controller = controller;
    current.inFlight = true;
    setState(previous => ({ ...previous, loading: true }));
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    const isCurrent = () => current.mounted && generation === current.generation;
    let readingCatalog = reloadAccounts || !current.catalogLoaded;
    try {
      const options: RequestInit = {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      };
      if (readingCatalog) {
        const response = await fetch('/api/accounts', options);
        const payload: unknown = await response.json();
        if (!isCurrent()) return;
        if (!response.ok) {
          clearTrades();
          setState(previous => ({ ...previous, ...clearedAccount,
            historyPlaceholder: { state: 'error', message: '계좌 설정을 확인해 주세요' } }));
          fail(message(record(payload)?.error, '계좌 목록을 불러오지 못했습니다. 설정을 확인한 뒤 새로고침해 주세요.'));
          return;
        }
        const catalog = readCatalog(payload);
        current.accounts = catalog.accounts;
        current.catalogLoaded = true;
        if (!current.selectionInitialized) {
          current.selectedId = catalog.defaultId;
          current.selectionInitialized = true;
        }
        setState(previous => ({ ...previous, accounts: catalog.accounts, selectedId: current.selectedId, catalogLoaded: true }));
        readingCatalog = false;
      }

      const account = current.accounts.find(account => account.id === current.selectedId);
      if (!account?.configured) {
        clearTrades();
        setState(previous => ({
          ...previous, ...clearedAccount,
          historyPlaceholder: { state: 'empty', message: account ? '계좌 설정이 필요합니다' : '조회할 계좌를 선택해 주세요' },
          error: account ? '이 계좌는 설정이 필요해요. 계좌 설정을 완료한 뒤 새로고침해 주세요.'
            : current.selectedId ? '선택했던 계좌가 목록에 없어요. 조회할 계좌를 다시 선택해 주세요.'
              : '조회할 계좌를 선택해 주세요. 등록된 계좌가 없다면 계좌 설정 후 새로고침해 주세요.',
          emptyTitle: account ? '계좌 설정이 필요해요' : '계좌를 선택해 주세요',
          status: 'stale', statusLabel: '계좌 선택 확인', announcement: '계좌 조회에 실패했습니다.',
        }));
        return;
      }

      const requestAccountId = account.id;
      void refreshTrades(requestAccountId);
      const response = await fetch(`/api/account?account=${encodeURIComponent(requestAccountId)}`, options);
      const payload: unknown = await response.json();
      if (!isCurrent() || requestAccountId !== current.selectedId) return;
      if (response.status === 404) {
        clearTrades();
        setState(previous => ({ ...previous, ...clearedAccount }));
        fail('선택한 계좌를 찾을 수 없어요. 새로고침 후 계좌를 다시 선택해 주세요.');
        return;
      }
      const data = record(payload);
      if (!data || (data.status !== 'ok' && data.status !== 'error')) throw new Error('Invalid response');
      if (record(data.account)?.id !== requestAccountId) {
        clearTrades();
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
        ...previous, history, historyPlaceholder: { state: 'error', message: '이력을 불러오지 못했습니다' },
        ...(snapshot ? { snapshot, emptyTitle: '아직 보유한 주식이 없어요' } : {}),
      }));
      if (!response.ok || data.status === 'error' || data.stale) {
        fail(message(data.error, '계좌 정보를 갱신하지 못했습니다.'));
      } else if (snapshot) {
        setState(previous => ({ ...previous, error: null, status: 'connected', statusLabel: '계좌 연결됨',
          announcement: '계좌 정보가 갱신되었습니다.' }));
      } else {
        throw new Error('Missing account snapshot');
      }
    } catch {
      if (!isCurrent()) return;
      if (readingCatalog) {
        clearTrades();
        setState(previous => ({ ...previous, ...clearedAccount,
          historyPlaceholder: { state: 'error', message: '계좌 목록을 확인해 주세요' } }));
      }
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
  }, [fail, refreshTrades, clearTrades]);

  const selectAccount = useCallback((id: string) => {
    const current = session.current;
    if (current.selectedId === id || !current.accounts.some(account => account.id === id && account.configured)) return;
    current.selectedId = id;
    clearTrades();
    current.selectionInitialized = true;
    try { localStorage.setItem(ACCOUNT_STORAGE_KEY, id); } catch { /* Selection also works without storage. */ }
    setState(previous => ({ ...previous, ...clearedAccount, selectedId: id }));
    void refresh({ switchAccount: true });
  }, [refresh, clearTrades]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    void refresh();
    return () => {
      current.mounted = false;
      ++current.generation;
      current.controller?.abort();
      window.clearTimeout(current.timeout);
      current.controller = null;
      current.timeout = undefined;
      current.inFlight = false;
    };
  }, [refresh]);

  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearInterval(timer);
      timer = autoRefresh && !document.hidden ? window.setInterval(() => { void refresh(); }, REFRESH_MS) : undefined;
    };
    const onVisibility = () => {
      schedule();
      if (autoRefresh && !document.hidden) void refresh();
    };
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility); };
  }, [autoRefresh, refresh]);

  return { ...state, autoRefresh, setAutoRefresh, selectAccount, refresh, trades };
}
