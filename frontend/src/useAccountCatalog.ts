import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { AccountChoice } from './types';

const ACCOUNT_STORAGE_KEY = 'kis-dashboard-account';
const LOAD_ERROR = '계좌 목록 조회 실패 · 설정 확인 필요';

function savedSelection(): string | null {
  try { return localStorage.getItem(ACCOUNT_STORAGE_KEY) || null; }
  catch { return null; }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function readCatalog(value: unknown): { accounts: AccountChoice[]; defaultId: string | null } {
  const catalog = record(value);
  if (!catalog || !Array.isArray(catalog.accounts)) throw new Error(LOAD_ERROR);
  const ids = new Set<string>();
  const accounts = catalog.accounts.map((value: unknown): AccountChoice => {
    const account = record(value);
    if (!account || typeof account.id !== 'string' || !account.id || ids.has(account.id)
      || typeof account.name !== 'string' || typeof account.configured !== 'boolean') throw new Error(LOAD_ERROR);
    ids.add(account.id);
    return { id: account.id, name: account.name, configured: account.configured };
  });
  return { accounts, defaultId: typeof catalog.default_account === 'string' ? catalog.default_account : null };
}

export default function useAccountCatalog(enabled = true) {
  const [initialSelection] = useState(savedSelection);
  const [state, setState] = useState({ accounts: [] as AccountChoice[], selectedId: initialSelection,
    loaded: false, loading: enabled, error: null as string | null, revision: 0 });
  const session = useRef({ mounted: false, enabled, generation: 0, needsRefresh: true,
    selectedId: initialSelection, selectionInitialized: initialSelection !== null, accounts: [] as AccountChoice[],
    controller: null as AbortController | null, timeout: undefined as number | undefined });

  const cancel = useCallback(() => {
    const current = session.current;
    ++current.generation;
    if (current.controller) current.needsRefresh = true;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
    setState(previous => previous.loading ? { ...previous, loading: false } : previous);
  }, []);

  const refresh = useCallback(async () => {
    const current = session.current;
    if (!current.mounted || !current.enabled || document.hidden || current.controller) return;
    const generation = ++current.generation;
    const controller = new AbortController();
    current.controller = controller;
    current.needsRefresh = true;
    setState(previous => ({ ...previous, loading: true }));
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    const isCurrent = () => current.mounted && current.enabled && !document.hidden && generation === current.generation;
    let failureMessage = LOAD_ERROR;
    try {
      const response = await fetch('/api/accounts', {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error(LOAD_ERROR);
      if (!response.ok) {
        const error = record(payload)?.error;
        failureMessage = typeof error === 'string' && error ? error : LOAD_ERROR;
        throw new Error(failureMessage);
      }
      const catalog = readCatalog(payload);
      current.accounts = catalog.accounts;
      if (!current.selectionInitialized) {
        current.selectedId = catalog.defaultId;
        current.selectionInitialized = true;
      }
      current.needsRefresh = false;
      setState(previous => ({ accounts: catalog.accounts, selectedId: current.selectedId,
        loaded: true, loading: false, error: null, revision: previous.revision + 1 }));
    } catch {
      if (!isCurrent()) return;
      current.accounts = [];
      setState(previous => ({ ...previous, accounts: [], loaded: false, error: failureMessage }));
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        current.controller = null;
        current.timeout = undefined;
        setState(previous => ({ ...previous, loading: false }));
      }
    }
  }, []);

  const isReady = useCallback(() => !session.current.needsRefresh && session.current.controller === null, []);

  const selectAccount = useCallback((id: string) => {
    const current = session.current;
    if (id === current.selectedId || !current.accounts.some(account => account.id === id && account.configured)) return;
    current.selectedId = id;
    current.selectionInitialized = true;
    try { localStorage.setItem(ACCOUNT_STORAGE_KEY, id); } catch { /* Selection also works without storage. */ }
    setState(previous => ({ ...previous, selectedId: id }));
  }, []);

  useLayoutEffect(() => {
    session.current.mounted = true;
    session.current.enabled = enabled;
    if (!enabled) cancel();
    return () => { session.current.mounted = false; cancel(); };
  }, [enabled, cancel]);

  useEffect(() => {
    if (!enabled) return;
    void refresh();
    const onVisibility = () => {
      if (document.hidden) cancel();
      else if (session.current.needsRefresh) void refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [enabled, refresh, cancel]);

  return { ...state, selectAccount, refresh, isReady };
}
