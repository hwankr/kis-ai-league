import { useCallback, useEffect, useRef, useState } from 'react';
import { readExperiments } from './experiments';
import type { ExperimentCommand, ExperimentData } from './experiments';

const LOAD_ERROR = '실험실을 불러오지 못했습니다.';
const ACTION_ERROR = '요청 결과를 확인하지 못했습니다. 상태를 다시 조회해 주세요.';

export default function useExperiments() {
  const [data, setData] = useState<ExperimentData | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<ExperimentCommand['action'] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const session = useRef({ mounted: false, generation: 0, controller: null as AbortController | null,
    mutation: false, timeout: undefined as number | undefined });

  const cancel = useCallback(() => {
    const current = session.current;
    ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
    current.mutation = false;
  }, []);

  const request = useCallback(async (command?: ExperimentCommand): Promise<boolean> => {
    const current = session.current;
    if (!current.mounted || current.mutation || document.hidden || (!command && current.controller)) return false;
    if (command) cancel();
    const generation = ++current.generation;
    const controller = new AbortController();
    const isCurrent = () => current.mounted && generation === current.generation;
    current.controller = controller;
    current.mutation = !!command;
    setLoading(!command);
    setPending(command?.action ?? null);
    const timeout = window.setTimeout(() => {
      if (!isCurrent()) return;
      cancel();
      setError(command ? ACTION_ERROR : LOAD_ERROR);
      setLoading(false);
      setPending(null);
    }, 25_000);
    current.timeout = timeout;
    try {
      const response = await fetch('/api/experiments', {
        method: command ? 'POST' : 'GET', headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1',
          ...(command ? { 'Content-Type': 'application/json' } : {}) },
        ...(command ? { body: JSON.stringify(command) } : {}), cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return false;
      if (!response.ok) {
        const message = payload && typeof payload === 'object' && 'error' in payload && typeof payload.error === 'string'
          ? payload.error : command ? ACTION_ERROR : LOAD_ERROR;
        setError(message);
        return false;
      }
      const next = readExperiments(payload);
      setData(next);
      setError(next.error);
      return true;
    } catch {
      if (isCurrent()) setError(command ? ACTION_ERROR : LOAD_ERROR);
      return false;
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        current.controller = null;
        current.mutation = false;
        current.timeout = undefined;
        setLoading(false);
        setPending(null);
      }
    }
  }, [cancel]);
  const refresh = useCallback(() => request(), [request]);
  const execute = useCallback((command: ExperimentCommand) => request(command), [request]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    let timer: number | undefined;
    const onVisibility = () => {
      window.clearInterval(timer);
      timer = undefined;
      if (document.hidden) {
        // A submitted command may finish while hidden; visibility must not resubmit it.
        if (!current.mutation) { cancel(); setLoading(false); }
      } else {
        void refresh();
        timer = window.setInterval(() => { void refresh(); }, 20_000);
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

  return { data, loading, pending, error, refresh, execute };
}
