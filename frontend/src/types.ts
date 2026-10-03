export type Numeric = string | number | null;

export interface AccountChoice { id: string; name: string; configured: boolean }
export interface AccountCatalog { default_account: string; accounts: AccountChoice[] }
export interface Summary {
  total_value?: Numeric;
  cash?: Numeric;
  securities_value?: Numeric;
  purchase_amount?: Numeric;
  unrealized_pnl?: Numeric;
  unrealized_return_pct?: Numeric;
}
export interface Holding {
  symbol: string;
  name: string;
  quantity: Numeric;
  avg_price: Numeric;
  price: Numeric;
  market_value: Numeric;
  purchase_amount: Numeric;
  pnl: Numeric;
  return_pct: Numeric;
}
export interface HistoryPoint { observed_at: string; total_value: Numeric; cash: Numeric }
export interface HistoryData { points: HistoryPoint[]; total_count: number; error: string | null }
export interface HistoryPlaceholder { state: 'loading' | 'empty' | 'error'; message: string }
export interface AccountSnapshot {
  status: 'ok' | 'error';
  environment: string;
  account: { id: string; name: string };
  updated_at: string | null;
  refresh_interval_seconds: number;
  stale: boolean;
  error: string | null;
  summary: Summary;
  holdings: Holding[];
  history: HistoryData;
}

export interface TradeRange { start: string; end: string }
export interface Trade {
  order_date: string;
  order_id: string;
  branch_id: string;
  symbol: string;
  name: string;
  side: 'buy' | 'sell';
  quantity: string;
  price: string;
  amount: string;
  order_time: string | null;
}
export interface TradeHistory {
  status: 'ok' | 'error';
  account: { id: string; name: string };
  environment: string;
  start_date: string;
  end_date: string;
  trades: Trade[];
  total_count: number;
  updated_at: string | null;
  stale: boolean;
  error: string | null;
}

export interface MarketQuote {
  symbol: string;
  name?: string;
  market: 'KRX';
  environment: 'paper';
  price: string | null;
  change_percent: string | null;
  volume: string | null;
  cumulative_turnover: string | null;
  observed_at: string | null;
  last_attempt_at: string | null;
  error: string | null;
  total_count: number;
}
export interface MarketData {
  status: 'ok' | 'error';
  error: string | null;
  collector: {
    state: 'running' | 'stopped' | 'stale' | 'not_started';
    heartbeat_at: string | null;
    interval_seconds: number;
    next_run_at: string | null;
    error: string | null;
  };
  symbols: string[];
  quotes: MarketQuote[];
}
