import { useCallback, useEffect, useRef, useState } from 'react';
import { numeric } from './format';
import type { MarketData, MarketQuote } from './types';

const LOAD_ERROR = '시세 수집 상태를 불러오지 못했습니다.';

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function validTime(value: unknown): boolean {
  return value === null || typeof value === 'string' && Number.isFinite(Date.parse(value));
}

function validError(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function symbolsOf(value: unknown): string[] | null {
  return Array.isArray(value) && value.every(symbol => typeof symbol === 'string' && /^\d{6}$/.test(symbol))
    && new Set(value).size === value.length ? value as string[] : null;
}

function readMarket(data: Record<string, unknown>): MarketData {
  const symbols = symbolsOf(data.symbols);
  const collector = record(data.collector);
  if (!symbols || (data.status !== 'ok' && data.status !== 'error') || !validError(data.error) || !collector
    || !['running', 'stopped', 'stale', 'not_started'].includes(String(collector.state))
    || !validTime(collector.heartbeat_at) || !validTime(collector.next_run_at) || !validError(collector.error)
    || typeof collector.interval_seconds !== 'number' || !Number.isFinite(collector.interval_seconds) || collector.interval_seconds <= 0
    || !Array.isArray(data.quotes) || data.quotes.length !== symbols.length
      && !(data.status === 'error' && data.quotes.length === 0)) throw new Error('Invalid market status');
  const quotes = data.quotes.map((value: unknown): MarketQuote => {
    const quote = record(value);
    if (!quote || typeof quote.symbol !== 'string' || !symbols.includes(quote.symbol) || quote.market !== 'KRX'
      || quote.environment !== 'paper' || (quote.name !== undefined && typeof quote.name !== 'string')
      || !validTime(quote.observed_at) || !validTime(quote.last_attempt_at) || !validError(quote.error)
      || typeof quote.total_count !== 'number' || !Number.isSafeInteger(quote.total_count) || quote.total_count < 0
      || ['price', 'volume', 'cumulative_turnover'].some(key => quote[key] !== null
        && (typeof quote[key] !== 'string' || (numeric(quote[key]) ?? -1) < 0))
      || quote.change_percent !== null && (typeof quote.change_percent !== 'string' || numeric(quote.change_percent) === null)
      || quote.observed_at === null && (quote.price !== null || quote.total_count !== 0)
      || quote.observed_at !== null && (quote.price === null || quote.total_count < 1)) throw new Error('Invalid quote');
    return quote as unknown as MarketQuote;
  });
  if (new Set(quotes.map(quote => quote.symbol)).size !== quotes.length) throw new Error('Duplicate quote');
  return { ...data, quotes: quotes.length ? symbols.map(symbol => quotes.find(quote => quote.symbol === symbol)!) : [] } as unknown as MarketData;
}

export default function useMarketData(autoRefresh: boolean) {
  const [data, setData] = useState<MarketData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const session = useRef({ mounted: false, generation: 0, controller: null as AbortController | null,
    timeout: undefined as number | undefined });

  const refresh = useCallback(async () => {
    const current = session.current;
    if (!current.mounted || current.controller || document.hidden) return;
    const generation = ++current.generation;
    const controller = new AbortController();
    current.controller = controller;
    setLoading(true);
    const isCurrent = () => current.mounted && current.generation === generation;
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    try {
      const response = await fetch('/api/market', {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error('Request timed out');
      const raw = record(payload);
      const symbols = symbolsOf(raw?.symbols);
      // A changed watchlist must never retain rows for removed symbols, even when its data is malformed.
      if (symbols) setData(previous => previous && previous.symbols.join(',') !== symbols.join(',') ? null : previous);
      if (!raw) throw new Error('Invalid market response');
      const market = readMarket(raw);
      setData(previous => market.status === 'error' && !market.quotes.length && previous
        && previous.symbols.join(',') === market.symbols.join(',') ? { ...market, quotes: previous.quotes } : market);
      setError(!response.ok || market.status === 'error' ? market.error || LOAD_ERROR : null);
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
  }, []);

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
    };
  }, [refresh]);

  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      window.clearInterval(timer);
      timer = autoRefresh && !document.hidden ? window.setInterval(() => { void refresh(); }, 30_000) : undefined;
    };
    const onVisibility = () => {
      schedule();
      if (autoRefresh && !document.hidden) void refresh();
    };
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility); };
  }, [autoRefresh, refresh]);

  return { data, loading, error, refresh };
}
