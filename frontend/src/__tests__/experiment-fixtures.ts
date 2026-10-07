import type { AutonomyData, ExperimentData, ExperimentOrder, ExperimentRun } from '../experiments';

export function experimentRun(overrides: Partial<ExperimentRun> = {}): ExperimentRun {
  return { id: 'run-1', as_of: '2026-10-02', created_at: '2026-10-05T00:00:00Z', status: 'complete', input_hash: 'fixture-input-hash',
    signals: [{ strategy_id: 'rules', symbol: '005930', name: '삼성전자', action: 'buy', score: 75, reason: '거래대금과 추세 조건 충족', evidence_ids: ['daily:005930:2026-10-02'] }], error: null, ...overrides };
}
export function experimentOrder(overrides: Partial<ExperimentOrder> = {}): ExperimentOrder {
  return { id: 'local-1', strategy_id: 'rules', symbol: '005930', name: '삼성전자', side: 'buy', quantity: 3,
    limit_price: '100000', filled_quantity: 1, average_price: '99900', status: 'partial', order_id: '0000001', created_at: '2026-10-05T00:01:00Z', error: null, ...overrides };
}
export function experiments(overrides: Partial<ExperimentData> = {}): ExperimentData {
  return { status: 'idle', busy: false, environment: 'paper', updated_at: '2026-10-05T00:02:00Z', error: null,
    llm: { configured: false, provider: 'codex', model: null, error: null },
    policy: { account_id: null, budget: null, order_cap: null, daily_buy_limit: null, execution_strategy: null },
    automation: { enabled: false, state: 'paused', pause_reason: null },
    strategies: [{ id: 'rules', label: '추세 규칙', description: '가격·거래량 조건', version: 'rules-v1' },
      { id: 'llm', label: 'LLM 판단', description: '출처를 포함한 판단', version: 'llm-v1' },
      { id: 'hybrid', label: '혼합 전략', description: '규칙과 LLM 결합', version: 'hybrid-v1' }],
    runs: [], orders: [], positions: [], metrics: [], events: [], ...overrides };
}
export const configuredPolicy = { account_id: 'growth', budget: '3000000', order_cap: '500000', daily_buy_limit: '1000000', execution_strategy: 'rules' };
export function autonomy(overrides: Partial<AutonomyData> = {}): AutonomyData {
  return { status: 'healthy', last_heartbeat_at: '2026-10-05T01:00:00Z', last_account_at: '2026-10-05T00:59:00Z',
    next_retry_at: null, error: null, issues: [], performance: { as_of: '2026-10-05T00:59:00Z', baseline: '10000000',
      total_value: '10125000', cash: '8000000', return_pct: '1.25', max_drawdown_pct: '-0.5', observations: 12 },
    daily_reports: [{ date: '2026-10-05', created_at: '2026-10-05T01:00:00Z', total_value: '10125000', return_pct: '1.25', orders: 5, filled_orders: 3, issues: 0 }], ...overrides };
}
