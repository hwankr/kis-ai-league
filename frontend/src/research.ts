export type ObservationClass = 'prospective' | 'bootstrap' | 'late' | 'timing_unverified';
export type ObservationStatus = 'excluded' | 'pending_entry' | 'open' | 'closed' | 'unknown' | 'fill_unverifiable';

export interface ResearchObservation {
  symbol: string;
  name: string;
  board: string;
  signal_date: string;
  generated_at: string;
  classification: ObservationClass;
  original_classification?: ObservationClass;
  eligible: boolean | null;
  signal: boolean;
  trend: boolean | null;
  reason: string | null;
  input_sha256: string;
  outcome: {
    status: ObservationStatus;
    entry_date: string | null;
    entry_observed_at: string | null;
    exit_date: string | null;
    exit_observed_at: string | null;
    holding_days: number | null;
    returns: { slippage: number; net_return: number | null }[];
    reason: string | null;
  };
}

export interface ResearchData {
  status: 'idle' | 'collecting' | 'ready' | 'partial' | 'error';
  rule_id: string;
  research_status: 'unadopted';
  version_id: string | null;
  frozen_at: string | null;
  as_of: string | null;
  observed_at: string | null;
  error: string | null;
  order_enabled: false;
  allocation: null;
  counts: { signals: number; prospective: number; bootstrap: number; late: number; timing_unverified?: number; closed: number; open: number; unknown: number };
  observations: ResearchObservation[];
  comparison: {
    signal_count: number;
    control_count: number;
    signal_mean: number | null;
    control_mean: number | null;
    edge: number | null;
    pending: boolean;
    descriptive_only: true;
    paired_days?: number;
    paired_groups?: number;
  };
  next_refresh_at: string | null;
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
function finite(value: unknown): boolean {
  return value === null || typeof value === 'number' && Number.isFinite(value);
}
function text(value: unknown): boolean { return value === null || typeof value === 'string'; }
function time(value: unknown): boolean {
  return value === null || typeof value === 'string' && Number.isFinite(Date.parse(value));
}
function date(value: unknown): boolean {
  return value === null || typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}

export function readResearch(payload: unknown): ResearchData {
  if (!record(payload) || !['idle', 'collecting', 'ready', 'partial', 'error'].includes(String(payload.status))
    || payload.research_status !== 'unadopted' || payload.order_enabled !== false || payload.allocation !== null
    || typeof payload.rule_id !== 'string' || !text(payload.version_id)
    || !time(payload.frozen_at) || !date(payload.as_of) || !time(payload.observed_at) || !time(payload.next_refresh_at)
    || !text(payload.error) || !record(payload.counts) || !Array.isArray(payload.observations)) throw new Error('Invalid research response');
  const counts = payload.counts;
  if (['signals', 'prospective', 'bootstrap', 'late', 'closed', 'open', 'unknown'].some(key => !count(counts[key]))) {
    throw new Error('Invalid observation counts');
  }
  if (counts.timing_unverified !== undefined && !count(counts.timing_unverified)) throw new Error('Invalid timing count');
  for (const row of payload.observations) {
    if (!record(row) || typeof row.symbol !== 'string' || !/^[0-9A-Z]{6}$/.test(row.symbol)
      || typeof row.name !== 'string' || typeof row.board !== 'string'
      || typeof row.signal_date !== 'string' || !date(row.signal_date)
      || typeof row.generated_at !== 'string' || !time(row.generated_at)
      || !['prospective', 'bootstrap', 'late', 'timing_unverified'].includes(String(row.classification))
      || row.original_classification !== undefined && !['prospective', 'bootstrap', 'late', 'timing_unverified'].includes(String(row.original_classification))
      || ![true, false, null].includes(row.eligible as boolean | null) || typeof row.signal !== 'boolean'
      || ![true, false, null].includes(row.trend as boolean | null) || !text(row.reason)
      || typeof row.input_sha256 !== 'string' || !record(row.outcome)) throw new Error('Invalid research observation');
    const outcome = row.outcome;
    if (!['excluded', 'pending_entry', 'open', 'closed', 'unknown', 'fill_unverifiable'].includes(String(outcome.status))
      || !date(outcome.entry_date) || !date(outcome.exit_date) || !time(outcome.entry_observed_at) || !time(outcome.exit_observed_at)
      || outcome.holding_days !== null && !count(outcome.holding_days)
      || !text(outcome.reason) || !Array.isArray(outcome.returns)
      || outcome.returns.some(value => !record(value) || typeof value.slippage !== 'number'
        || !Number.isFinite(value.slippage) || value.slippage < 0 || !finite(value.net_return))) throw new Error('Invalid observation outcome');
  }
  const comparison = payload.comparison;
  if (!record(comparison) || !count(comparison.signal_count) || !count(comparison.control_count)
    || !finite(comparison.signal_mean) || !finite(comparison.control_mean) || !finite(comparison.edge)
    || typeof comparison.pending !== 'boolean' || comparison.descriptive_only !== true
    || comparison.paired_days !== undefined && !count(comparison.paired_days)
    || comparison.paired_groups !== undefined && !count(comparison.paired_groups)) throw new Error('Invalid research comparison');
  return payload as unknown as ResearchData;
}
