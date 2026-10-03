import { useCallback, useEffect, useRef, useState } from 'react';
import { numeric } from './format';
import type { Trade, TradeHistory, TradeRange } from './types';

const DAY_MS = 86_400_000;
const LOAD_ERROR = '거래 내역을 불러오지 못했습니다. 다시 조회해 주세요.';

export function todayInSeoul(now = new Date()): string {
  return new Date(now.getTime() + 9 * 60 * 60 * 1_000).toISOString().slice(0, 10);
}

export function defaultTradeRange(now = new Date()): TradeRange {
  const end = todayInSeoul(now);
  return { start: new Date(Date.parse(end) - 29 * DAY_MS).toISOString().slice(0, 10), end };
}

function validDate(value: unknown): value is string {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}

export function tradeRangeError(range: TradeRange): string | null {
  if (!validDate(range.start) || !validDate(range.end)) return '조회할 날짜를 입력해 주세요.';
  if (range.start > range.end) return '시작일은 종료일 이전이어야 합니다.';
  if (range.end > todayInSeoul()) return '오늘까지 조회할 수 있습니다.';
  if ((Date.parse(range.end) - Date.parse(range.start)) / DAY_MS >= 90) return '조회 기간은 최대 90일입니다.';
  return null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function readTrades(data: Record<string, unknown>): TradeHistory {
  if (!Array.isArray(data.trades) || typeof data.total_count !== 'number' || !Number.isInteger(data.total_count)
    || data.total_count < data.trades.length || (data.updated_at !== null
      && (typeof data.updated_at !== 'string' || !Number.isFinite(Date.parse(data.updated_at))))) {
    throw new Error('Invalid trade history');
  }
  const trades = data.trades.map((value: unknown): Trade => {
    const trade = record(value);
    if (!trade || !validDate(trade.order_date) || typeof trade.order_id !== 'string' || !trade.order_id
      || typeof trade.branch_id !== 'string' || typeof trade.symbol !== 'string' || !trade.symbol
      || typeof trade.name !== 'string' || (trade.side !== 'buy' && trade.side !== 'sell')
      || typeof trade.quantity !== 'string' || (numeric(trade.quantity) ?? 0) <= 0
      || typeof trade.price !== 'string' || (numeric(trade.price) ?? -1) < 0
      || typeof trade.amount !== 'string' || (numeric(trade.amount) ?? -1) < 0
      || (trade.order_time !== null && typeof trade.order_time !== 'string')) {
      throw new Error('Invalid trade');
    }
    return trade as unknown as Trade;
  });
  return { ...data, trades } as unknown as TradeHistory;
}

export default function useTradeHistory() {
  const [range, setRange] = useState(defaultTradeRange);
  const [data, setData] = useState<TradeHistory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accountId, setAccountId] = useState<string | null>(null);
  const session = useRef({
    accountId: null as string | null, range, generation: 0, mounted: true,
    controller: null as AbortController | null, timeout: undefined as number | undefined,
  });

  const clear = useCallback(() => {
    const current = session.current;
    ++current.generation;
    current.accountId = null;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    current.controller = null;
    current.timeout = undefined;
    setData(null);
    setError(null);
    setLoading(false);
    setAccountId(null);
  }, []);

  const refresh = useCallback(async (accountId: string, nextRange?: TradeRange) => {
    const current = session.current;
    if (!current.mounted || document.hidden) return;
    const requestRange = nextRange ?? current.range;
    if (tradeRangeError(requestRange)) return;
    const changed = current.accountId !== accountId || current.range.start !== requestRange.start || current.range.end !== requestRange.end;
    const generation = ++current.generation;
    current.controller?.abort();
    window.clearTimeout(current.timeout);
    const controller = new AbortController();
    current.accountId = accountId;
    setAccountId(accountId);
    current.range = requestRange;
    current.controller = controller;
    setRange(requestRange);
    if (changed) { setData(null); setError(null); }
    setLoading(true);
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    const isCurrent = () => current.mounted && current.generation === generation && current.accountId === accountId;
    try {
      const query = new URLSearchParams({ account: accountId, start: requestRange.start, end: requestRange.end });
      const response = await fetch(`/api/trades?${query}`, {
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1' }, cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error('Request timed out');
      const raw = record(payload);
      if (!raw || record(raw.account)?.id !== accountId || raw.start_date !== requestRange.start || raw.end_date !== requestRange.end) {
        setData(null);
        setError(LOAD_ERROR);
        return;
      }
      if (raw.status !== 'ok' && raw.status !== 'error') throw new Error('Invalid response');
      // An uncached failure belongs to the current credential identity; discard earlier values.
      if (raw.status === 'error' && !raw.updated_at && !raw.stale) setData(null);
      const history = readTrades(raw);
      if (history.trades.some(trade => trade.order_date < requestRange.start || trade.order_date > requestRange.end)) {
        setData(null);
        setError(LOAD_ERROR);
        return;
      }
      setData(history);
      setError(!response.ok || history.status === 'error' || history.stale ? history.error || LOAD_ERROR : null);
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

  const query = useCallback((nextRange: TradeRange) => {
    const current = session.current;
    if (current.accountId) void refresh(current.accountId, nextRange);
  }, [refresh]);

  useEffect(() => {
    const current = session.current;
    current.mounted = true;
    return () => {
      current.mounted = false;
      ++current.generation;
      current.controller?.abort();
      window.clearTimeout(current.timeout);
    };
  }, []);

  return { data, range, loading, error, accountId, refresh, clear, query };
}
