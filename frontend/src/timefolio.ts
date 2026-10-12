import { numeric } from './format';
import type { Numeric } from './types';

export interface TimefolioHolding {
  symbol: string; name: string | null; quantity: Numeric; avg_price: Numeric; price: Numeric;
  market_value: Numeric; purchase_amount: Numeric; pnl: Numeric; return_pct: Numeric;
  weight_pct: Numeric; change_pct: Numeric;
}
export interface TimefolioOrder {
  order_id: string; symbol: string; name: string | null; side: 'buy' | 'sell'; status: string;
  weight: Numeric; filled_quantity: Numeric; created_at: string | null; order_time: string | null;
  average_price: Numeric; verified: boolean; system_owned: boolean;
}
export interface TimefolioData {
  status: 'ok' | 'waiting' | 'error'; stale: boolean; error: string | null;
  updated_at: string | null; last_attempt_at: string | null; refresh_interval_seconds: number;
  account: { name: string; contest: string; session_date: string } | null;
  summary: { nav: Numeric; nav_change: Numeric; nav_change_pct: Numeric; rank: Numeric; participants: Numeric;
    holdings_value: Numeric; holdings_pnl: Numeric; holdings_return_pct: Numeric } | null;
  holdings: TimefolioHolding[]; orders: TimefolioOrder[];
}

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string';
const instant = (value: unknown): value is string | null => value === null || typeof value === 'string' && Number.isFinite(Date.parse(value));
function readNumber(value: unknown): Numeric {
  if (value !== null && numeric(value) === null) throw new Error('타임폴리오 수치를 확인하지 못했습니다.');
  return value as Numeric;
}

export function readTimefolio(value: unknown): TimefolioData {
  if (!object(value) || !['ok', 'waiting', 'error'].includes(String(value.status))
    || typeof value.stale !== 'boolean' || !nullableString(value.error)
    || !instant(value.updated_at) || !instant(value.last_attempt_at)
    || typeof value.refresh_interval_seconds !== 'number' || !Number.isFinite(value.refresh_interval_seconds)
    || value.refresh_interval_seconds <= 0 || !Array.isArray(value.holdings) || !Array.isArray(value.orders)) {
    throw new Error('타임폴리오 응답을 확인하지 못했습니다.');
  }
  let account: TimefolioData['account'] = null;
  if (value.account !== null) {
    if (!object(value.account) || typeof value.account.name !== 'string' || !value.account.name
      || typeof value.account.contest !== 'string' || !value.account.contest
      || typeof value.account.session_date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.account.session_date)) {
      throw new Error('타임폴리오 계좌를 확인하지 못했습니다.');
    }
    account = { name: value.account.name, contest: value.account.contest, session_date: value.account.session_date };
  }
  let summary: TimefolioData['summary'] = null;
  if (value.summary !== null) {
    if (!object(value.summary)) throw new Error('타임폴리오 자산을 확인하지 못했습니다.');
    summary = { nav: readNumber(value.summary.nav), nav_change: readNumber(value.summary.nav_change),
      nav_change_pct: readNumber(value.summary.nav_change_pct), rank: readNumber(value.summary.rank),
      participants: readNumber(value.summary.participants), holdings_value: readNumber(value.summary.holdings_value),
      holdings_pnl: readNumber(value.summary.holdings_pnl), holdings_return_pct: readNumber(value.summary.holdings_return_pct) };
  }
  if ((!account && (summary || value.holdings.length || value.orders.length))
    || value.status === 'ok' && (!account || !summary || !value.updated_at)) {
    throw new Error('타임폴리오 계좌 응답이 일치하지 않습니다.');
  }
  const holdings = value.holdings.map((row: unknown): TimefolioHolding => {
    if (!object(row) || typeof row.symbol !== 'string' || !nullableString(row.name)) {
      throw new Error('타임폴리오 보유 종목을 확인하지 못했습니다.');
    }
    return { symbol: row.symbol, name: row.name, quantity: readNumber(row.quantity), avg_price: readNumber(row.avg_price),
      price: readNumber(row.price), market_value: readNumber(row.market_value), purchase_amount: readNumber(row.purchase_amount),
      pnl: readNumber(row.pnl), return_pct: readNumber(row.return_pct), weight_pct: readNumber(row.weight_pct),
      change_pct: readNumber(row.change_pct) };
  });
  const orders = value.orders.map((row: unknown): TimefolioOrder => {
    if (!object(row) || typeof row.order_id !== 'string' || typeof row.symbol !== 'string' || !nullableString(row.name)
      || !['buy', 'sell'].includes(String(row.side)) || typeof row.status !== 'string'
      || !instant(row.created_at) || !nullableString(row.order_time)
      || typeof row.verified !== 'boolean' || typeof row.system_owned !== 'boolean') {
      throw new Error('타임폴리오 주문을 확인하지 못했습니다.');
    }
    return { order_id: row.order_id, symbol: row.symbol, name: row.name, side: row.side as 'buy' | 'sell', status: row.status,
      weight: readNumber(row.weight), filled_quantity: readNumber(row.filled_quantity), created_at: row.created_at,
      order_time: row.order_time, average_price: readNumber(row.average_price), verified: row.verified, system_owned: row.system_owned };
  });
  return { status: value.status as TimefolioData['status'], stale: value.stale, error: value.error,
    updated_at: value.updated_at, last_attempt_at: value.last_attempt_at, refresh_interval_seconds: value.refresh_interval_seconds,
    account, summary, holdings, orders };
}
