import { useCallback, useEffect, useRef, useState } from 'react';
import { readCompetitionProgress, type CompetitionProgressData } from './competition-progress';

export default function useCompetitionProgress() {
  const [data, setData] = useState<CompetitionProgressData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const state = useRef({ mounted: false, request: null as AbortController | null });
  const refresh = useCallback(async () => {
    const current = state.current;
    if (!current.mounted || current.request || document.hidden) return;
    const controller = new AbortController();
    current.request = controller;
    setLoading(true);
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    try {
      const response = await fetch('/api/competition', { headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' },
        cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error();
      const result = readCompetitionProgress(await response.json());
      if (current.mounted && current.request === controller && !controller.signal.aborted) {
        setData(result); setError(null);
      }
    } catch {
      if (current.mounted && current.request === controller) setError('대회 요건 조회 실패');
    } finally {
      window.clearTimeout(timeout);
      if (current.mounted && current.request === controller) { current.request = null; setLoading(false); }
    }
  }, []);
  useEffect(() => {
    const current = state.current;
    current.mounted = true;
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, 30_000);
    const visible = () => {
      if (!document.hidden) void refresh();
      else { current.request?.abort(); current.request = null; setLoading(false); }
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      current.mounted = false; current.request?.abort(); current.request = null;
      window.clearInterval(interval); document.removeEventListener('visibilitychange', visible);
    };
  }, [refresh]);
  return { data, loading, error, refresh };
}
