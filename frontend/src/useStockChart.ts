import { useCallback, useEffect, useRef, useState } from 'react';
import { numeric } from './format';
import type { ChartInterval, ChartRequest, StockBar, StockChartData } from './types';

const LOAD_ERROR = '차트를 불러오지 못했습니다. 다시 조회해 주세요.';
const CACHE_LIMIT = 32;
const requestKey = ({ symbol, interval }: ChartRequest) => `${symbol}:${interval}`;

function remember(cache: Map<string, StockChartData>, data: StockChartData) {
  const key = requestKey(data);
  cache.delete(key);
  if (data.bars.length) cache.set(key, data);
  while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value!);
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function validDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}

function readChart(value: unknown, request: ChartRequest): StockChartData {
  const data = record(value);
  if (!data || data.symbol !== request.symbol || data.interval !== request.interval
    || (data.status !== 'ok' && data.status !== 'error') || data.market !== 'KRX' || data.environment !== 'paper'
    || data.source !== 'KIS' || typeof data.adjusted !== 'boolean' || typeof data.stale !== 'boolean'
    || (data.name !== undefined && typeof data.name !== 'string') || (data.error !== null && typeof data.error !== 'string')
    || (data.updated_at !== null && (typeof data.updated_at !== 'string' || !Number.isFinite(Date.parse(data.updated_at))))
    || (data.as_of !== null && !validDate(data.as_of)) || !Array.isArray(data.bars)) throw new Error('Invalid chart response');

  const bars = data.bars.map((value: unknown): StockBar => {
    const bar = record(value);
    if (!bar || typeof bar.time !== 'string' || !validDate(bar.time.slice(0, 10))
      || (request.interval === 'day' ? !validDate(bar.time) : !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$/.test(bar.time))
      || !Number.isFinite(Date.parse(bar.time)) || typeof bar.partial !== 'boolean'
      || ['open', 'high', 'low', 'close'].some(key => typeof bar[key] !== 'string' || (numeric(bar[key]) ?? 0) <= 0)
      || typeof bar.volume !== 'string' || (numeric(bar.volume) ?? -1) < 0
      || Number(bar.high) < Math.max(Number(bar.open), Number(bar.close), Number(bar.low))
      || Number(bar.low) > Math.min(Number(bar.open), Number(bar.close))) throw new Error('Invalid chart bar');
    return bar as unknown as StockBar;
  }).sort((left, right) => left.time.localeCompare(right.time));
  if (new Set(bars.map(bar => bar.time)).size !== bars.length
    || bars.length > 0 && (data.as_of !== bars.at(-1)!.time.slice(0, 10) || data.updated_at === null)) throw new Error('Invalid chart dates');
  return { ...data, bars } as unknown as StockChartData;
}

export default function useStockChart(enabled = true) {
  const [interval, setInterval] = useState<ChartInterval>('day');
  const [request, setRequest] = useState<ChartRequest | null>(null);
  const [data, setData] = useState<StockChartData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const session = useRef({ mounted: false, enabled: false, generation: 0, controller: null as AbortController | null,
    timeout: undefined as number | undefined, request: null as ChartRequest | null,
    pending: null as ChartRequest | null, cache: new Map<string, StockChartData>() });

  const cancel = useCallback(() => {
    const current = session.current;
    ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
  }, []);

  const query = useCallback(async (symbol: string, requestedInterval: ChartInterval, options: { refresh?: boolean } = {}) => {
    const current = session.current;
    const normalizedSymbol = symbol.trim().toUpperCase();
    if (!current.mounted || !current.enabled || !/^[0-9A-Z]{6}$/.test(normalizedSymbol)) return;
    const next = { symbol: normalizedSymbol, interval: requestedInterval };
    if (document.hidden) {
      current.pending = next;
      return;
    }
    current.pending = null;
    const generation = ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    const controller = new AbortController();
    current.controller = controller;
    current.request = next;
    const cached = current.cache.get(requestKey(next));
    if (cached) remember(current.cache, cached);
    setRequest(next);
    setInterval(requestedInterval);
    setData(cached ?? null);
    setError(null);
    setLoading(true);
    const isCurrent = () => current.mounted && current.enabled && !document.hidden && current.generation === generation;
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    current.timeout = timeout;
    try {
      const params = new URLSearchParams(next);
      if (options.refresh) params.set('refresh', '1');
      const response = await fetch(`/api/chart?${params}`, {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error('Request timed out');
      const chart = readChart(payload, next);
      const failed = !response.ok || chart.status === 'error';
      const updated = failed && !chart.bars.length && cached
        ? { ...cached, stale: true } : { ...chart, stale: chart.stale || failed };
      remember(current.cache, updated);
      setData(updated);
      setError(failed ? chart.error || LOAD_ERROR : null);
    } catch {
      if (isCurrent()) {
        const retained = cached ? { ...cached, stale: true } : null;
        if (retained) remember(current.cache, retained);
        setData(retained);
        setError(LOAD_ERROR);
      }
    } finally {
      window.clearTimeout(timeout);
      if (isCurrent()) {
        current.controller = null;
        current.timeout = undefined;
        setLoading(false);
      }
    }
  }, []);

  const refresh = useCallback(() => {
    const requested = session.current.request;
    if (requested) return query(requested.symbol, requested.interval, { refresh: true });
  }, [query]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    current.enabled = enabled;
    if (!enabled) {
      current.pending = null;
      setLoading(false);
    }
    const onVisibility = () => {
      if (document.hidden) {
        if (current.controller) current.pending = current.request;
        cancel();
        setLoading(false);
      } else if (enabled) {
        const requested = current.pending;
        if (requested) void query(requested.symbol, requested.interval);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      current.mounted = false;
      current.enabled = false;
      cancel();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [enabled, query, cancel]);

  return { data, request, interval, setInterval, loading, error, query, refresh };
}
