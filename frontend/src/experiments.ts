export interface ExperimentPolicy {
  account_id: string | null;
  budget: string | null;
  order_cap: string | null;
  daily_buy_limit: string | null;
  execution_strategy: string | null;
}
export interface ExperimentSignal {
  strategy_id: string; symbol: string; name: string;
  action: 'buy' | 'hold' | 'avoid'; score: number | null; reason: string; evidence_ids: string[];
}
export interface ExperimentRun {
  id: string; as_of: string | null; created_at: string; status: string; input_hash: string;
  signals: ExperimentSignal[]; error: string | null;
}
export interface ExperimentOrder {
  id: string; strategy_id: string; symbol: string; name: string; side: 'buy' | 'sell';
  quantity: number; limit_price: string; filled_quantity: number; average_price: string | null;
  status: string; order_id: string | null; created_at: string; error: string | null;
}
export interface AutonomyData {
  status: 'starting' | 'healthy' | 'degraded' | 'attention' | 'paused';
  last_heartbeat_at: string | null; last_account_at: string | null; next_retry_at: string | null; error: string | null;
  issues: { id: string; code: string; message: string; question: string | null; blocking: boolean;
    state: 'open' | 'resolved'; first_seen: string; last_seen: string }[];
  performance: { as_of: string | null; baseline: string | null; total_value: string | null; cash: string | null;
    return_pct: string | null; max_drawdown_pct: string | null; observations: number };
  daily_reports: { date: string; created_at: string; total_value: string | null; return_pct: string | null;
    orders: number; filled_orders: number; issues: number }[];
}
export interface ExperimentData {
  status: 'idle' | 'running' | 'error'; busy: boolean; environment: 'paper'; updated_at: string | null; error: string | null;
  llm: { configured: boolean; provider: string | null; model: string | null; error: string | null };
  policy: ExperimentPolicy;
  automation: { enabled: boolean; state: string; pause_reason: string | null };
  strategies: { id: string; label: string; description: string; version: string }[];
  runs: ExperimentRun[];
  orders: ExperimentOrder[];
  positions: { strategy_id: string; symbol: string; name: string; quantity: number; average_price: string }[];
  metrics: { strategy_id: string; closed_trades: number; realized_pnl: string; open_positions: number;
    shadow_closed?: number; shadow_open?: number; shadow_pending?: number; shadow_unknown?: number; shadow_excluded?: number; shadow_version_count?: number;
    shadow_net_pct?: string | null; shadow_stress_pct?: string | null }[];
  events: { at: string; kind: string; message: string }[];
  autonomy?: AutonomyData;
}
export type ExperimentCommand = { action: 'analyze' | 'start' | 'pause' | 'reconcile' }
  | { action: 'configure'; policy: ExperimentPolicy }
  | { action: 'cancel'; order_id: string }
  | { action: 'resolve'; id: string; broker_order_id: string; branch_id: string }
  | { action: 'answer'; id: string; answer: 'retry' | 'keep_paused' };

const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string';
const nullableText = (value: unknown) => value === null || text(value);
const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const money = (value: unknown) => text(value) && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value));
const nullableMoney = (value: unknown) => value === null || money(value);
const time = (value: unknown) => text(value) && /^\d{4}-\d{2}-\d{2}T/.test(value) && Number.isFinite(Date.parse(value));
const nullableTime = (value: unknown) => value === null || time(value);
const list = (value: unknown, valid: (row: Record<string, unknown>) => boolean) => Array.isArray(value) && value.every(row => object(row) && valid(row));
const strings = (value: unknown) => Array.isArray(value) && value.every(text);
const symbol = (value: unknown) => text(value) && /^[0-9A-Z]{6}$/.test(value);

export function readExperiments(value: unknown): ExperimentData {
  if (!object(value) || !['idle', 'running', 'error'].includes(String(value.status)) || typeof value.busy !== 'boolean'
    || value.environment !== 'paper' || !nullableTime(value.updated_at) || !nullableText(value.error)) throw new Error('실험실 응답 형식 확인 필요');
  const { llm, policy, automation } = value;
  if (!object(llm) || typeof llm.configured !== 'boolean' || !nullableText(llm.provider) || !nullableText(llm.model) || !nullableText(llm.error)
    || !object(policy) || !['account_id', 'budget', 'order_cap', 'daily_buy_limit', 'execution_strategy'].every(key => nullableText(policy[key]))
    || !object(automation) || typeof automation.enabled !== 'boolean' || !text(automation.state) || !nullableText(automation.pause_reason)
    || !list(value.strategies, row => ['id', 'label', 'description', 'version'].every(key => text(row[key])))
    || !list(value.runs, row => text(row.id) && nullableText(row.as_of) && time(row.created_at) && text(row.status) && text(row.input_hash)
      && nullableText(row.error) && list(row.signals, signal => text(signal.strategy_id) && symbol(signal.symbol) && text(signal.name)
        && ['buy', 'hold', 'avoid'].includes(String(signal.action)) && (signal.score === null || typeof signal.score === 'number' && Number.isFinite(signal.score))
        && text(signal.reason) && strings(signal.evidence_ids)))
    || !list(value.orders, row => text(row.id) && text(row.strategy_id) && symbol(row.symbol) && text(row.name)
      && ['buy', 'sell'].includes(String(row.side)) && count(row.quantity) && money(row.limit_price) && Number(row.limit_price) >= 0
      && count(row.filled_quantity) && Number(row.filled_quantity) <= Number(row.quantity)
      && (row.average_price === null || money(row.average_price)) && text(row.status) && nullableText(row.order_id)
      && time(row.created_at) && nullableText(row.error))
    || !list(value.positions, row => text(row.strategy_id) && symbol(row.symbol) && text(row.name) && count(row.quantity) && money(row.average_price))
    || !list(value.metrics, row => text(row.strategy_id) && count(row.closed_trades) && money(row.realized_pnl) && count(row.open_positions)
      && ['shadow_closed', 'shadow_open', 'shadow_pending', 'shadow_unknown', 'shadow_excluded', 'shadow_version_count'].every(key => row[key] === undefined || count(row[key]))
      && ['shadow_net_pct', 'shadow_stress_pct'].every(key => row[key] === undefined || row[key] === null || money(row[key])))
    || !list(value.events, row => time(row.at) && text(row.kind) && text(row.message))) throw new Error('실험실 응답 형식 확인 필요');
  if (value.autonomy !== undefined) {
    const autonomy = value.autonomy;
    if (!object(autonomy) || !['starting', 'healthy', 'degraded', 'attention', 'paused'].includes(String(autonomy.status))
      || !['last_heartbeat_at', 'last_account_at', 'next_retry_at'].every(key => nullableTime(autonomy[key])) || !nullableText(autonomy.error)
      || !list(autonomy.issues, issue => ['id', 'code', 'message'].every(key => text(issue[key])) && nullableText(issue.question)
        && typeof issue.blocking === 'boolean' && ['open', 'resolved'].includes(String(issue.state)) && time(issue.first_seen) && time(issue.last_seen))
      || !object(autonomy.performance) || !nullableText(autonomy.performance.as_of) || !count(autonomy.performance.observations)
      || !['baseline', 'total_value', 'cash', 'return_pct', 'max_drawdown_pct'].every(key => nullableMoney((autonomy.performance as Record<string, unknown>)[key]))
      || !list(autonomy.daily_reports, report => text(report.date) && time(report.created_at) && nullableMoney(report.total_value)
        && nullableMoney(report.return_pct) && ['orders', 'filled_orders', 'issues'].every(key => count(report[key])))) throw new Error('자동 운용 응답 형식 확인 필요');
  }
  return value as unknown as ExperimentData;
}
