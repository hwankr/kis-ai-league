import { useCallback, useEffect, useRef, useState } from 'react';
import { readResearch } from './research';
import type { ResearchData } from './research';

const LOAD_ERROR = '전진 관찰을 불러오지 못했습니다.';

export default function useResearchObservation() {
  const [data, setData] = useState<ResearchData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
    current.controller = controller;
    setLoading(true);
    const isCurrent = () => current.mounted && current.generation === generation;
    const timeout = window.setTimeout(() => {
      if (!isCurrent()) return;
      cancel();
      setError(LOAD_ERROR);
      setLoading(false);
    }, 25_000);
    current.timeout = timeout;
    try {
      const response = await fetch('/api/research', {
        method: 'GET', headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' },
        cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error(LOAD_ERROR);
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      const next = readResearch(payload);
      // A failed collector response can contain no rows; keep the last successful observation intact.
      setData(previous => next.status === 'error' && previous ? previous : next);
      setError(next.status === 'error' || next.status === 'partial' ? next.error || '일부 관찰 기록을 확인하지 못했습니다.' : next.error);
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
    let timer: number | undefined;
    const onVisibility = () => {
      window.clearInterval(timer);
      timer = undefined;
      if (document.hidden) {
        cancel();
        setLoading(false);
      } else {
        void refresh();
        timer = window.setInterval(() => { void refresh(); }, 30_000);
      }
    };
    onVisibility();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      current.mounted = false;
      cancel();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [cancel, refresh]);

  return { data, loading, error, refresh };
}
