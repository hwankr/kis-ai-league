import { useCallback, useEffect, useRef, useState } from 'react';
import { readTimefolio } from './timefolio';
import type { TimefolioData } from './timefolio';

const LOAD_ERROR = '타임폴리오 계좌 현황을 불러오지 못했습니다.';

export default function useTimefolioAccount() {
  const [data, setData] = useState<TimefolioData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const session = useRef({ mounted: false, generation: 0, controller: null as AbortController | null,
    timeout: undefined as number | undefined });

  const cancel = useCallback(() => {
    const current = session.current;
    ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
  }, []);

  const refresh = useCallback(async () => {
    const current = session.current;
    if (!current.mounted || current.controller || document.hidden) return;
    const generation = ++current.generation;
    const controller = new AbortController();
    const isCurrent = () => current.mounted && generation === current.generation;
    current.controller = controller;
    setLoading(true);
    const timeout = window.setTimeout(() => {
      if (!isCurrent()) return;
      cancel();
      setLoading(false);
      setError(LOAD_ERROR);
    }, 25_000);
    current.timeout = timeout;
    try {
      const response = await fetch('/api/timefolio', { method: 'GET', headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' },
        cache: 'no-store', signal: controller.signal });
      if (!isCurrent()) return;
      if (response.status === 404) { setError('타임폴리오 계좌 조회를 지원하는 서버 업데이트가 필요합니다.'); return; }
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      const snapshot = readTimefolio(payload);
      // An explicit empty account invalidates the previous account, even on errors.
      setData(snapshot);
      setError(snapshot.error || (snapshot.status === 'error' || !response.ok ? LOAD_ERROR : null));
    } catch {
      if (isCurrent()) setError(LOAD_ERROR);
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        current.controller = null;
        current.timeout = undefined;
        setLoading(false);
      }
    }
  }, [cancel]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    void refresh();
    return () => { current.mounted = false; cancel(); };
  }, [cancel, refresh]);

  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearInterval(timer);
      timer = autoRefresh && !document.hidden ? window.setInterval(() => { void refresh(); }, 30_000) : undefined;
    };
    const onVisibility = () => {
      if (document.hidden) { cancel(); setLoading(false); }
      else if (autoRefresh) void refresh();
      schedule();
    };
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility); };
  }, [autoRefresh, cancel, refresh]);

  return { data, loading, error, autoRefresh, setAutoRefresh, refresh };
}
