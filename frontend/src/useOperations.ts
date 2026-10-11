import { useCallback, useEffect, useRef, useState } from 'react';
import { readOperations } from './operations';
import type { OperationCommand, OperationsData } from './operations';

const LOAD_ERROR = '실행 상태를 불러오지 못했습니다.';
const ACTION_ERROR = '실행 요청 결과를 확인하지 못했습니다. 상태를 새로고침하세요.';
const UPDATE_ERROR = '서버 업데이트가 필요합니다. 현재 서버는 운용실 실행 제어를 지원하지 않습니다.';

export default function useOperations() {
  const [data, setData] = useState<OperationsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<OperationCommand['action'] | null>(null);
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

  const request = useCallback(async (command?: OperationCommand): Promise<boolean> => {
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
      const response = await fetch('/api/operations', {
        method: command ? 'POST' : 'GET',
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1',
          ...(command ? { 'Content-Type': 'application/json' } : {}) },
        ...(command ? { body: JSON.stringify(command) } : {}), cache: 'no-store', signal: controller.signal,
      });
      if (!isCurrent()) return false;
      if (response.status === 404) { setError(UPDATE_ERROR); return false; }
      const payload: unknown = await response.json();
      if (!isCurrent()) return false;
      if (!response.ok) {
        setError(payload && typeof payload === 'object' && 'error' in payload && typeof payload.error === 'string'
          ? payload.error : command ? ACTION_ERROR : LOAD_ERROR);
        return false;
      }
      setData(readOperations(payload));
      setError(null);
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
  const execute = useCallback((command: OperationCommand) => request(command), [request]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    let timer: number | undefined;
    const onVisibility = () => {
      window.clearInterval(timer);
      timer = undefined;
      if (document.hidden) {
        if (!current.mutation) { cancel(); setLoading(false); }
      } else {
        void refresh();
        timer = window.setInterval(() => { void refresh(); }, 5_000);
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
