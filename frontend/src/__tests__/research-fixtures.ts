import type { ResearchData, ResearchObservation } from '../research';

export function observation(overrides: Partial<ResearchObservation> = {}): ResearchObservation {
  return {
    symbol: '005930', name: '삼성전자', board: 'KOSPI', signal_date: '2026-10-05', generated_at: '2026-10-05T10:00:00Z',
    classification: 'prospective', eligible: true, signal: true, trend: true, reason: null, input_sha256: 'a'.repeat(64),
    outcome: { status: 'closed', entry_date: '2026-10-06', entry_observed_at: '2026-10-06T07:00:00Z',
      exit_date: '2026-10-13', exit_observed_at: '2026-10-13T07:00:00Z', holding_days: 5, reason: null,
      returns: [{ slippage: 0.001, net_return: 0.025 }, { slippage: 0.002, net_return: 0.022 }] },
    ...overrides,
  };
}

export function research(overrides: Partial<ResearchData> = {}): ResearchData {
  return {
    status: 'ready', rule_id: 'pullback-recovery', research_status: 'unadopted', version_id: 'fixed-v1',
    frozen_at: '2026-10-05T08:00:00Z', as_of: '2026-10-13', observed_at: '2026-10-13T10:00:00Z', error: null,
    order_enabled: false, allocation: null,
    counts: { signals: 1, prospective: 1, bootstrap: 0, late: 0, closed: 1, open: 0, unknown: 0 },
    observations: [observation()],
    comparison: { signal_count: 1, control_count: 3, signal_mean: 0.025, control_mean: 0.01, edge: 0.015,
      pending: false, descriptive_only: true, paired_days: 1, paired_groups: 1 },
    next_refresh_at: '2026-10-14T10:00:00Z', ...overrides,
  };
}
