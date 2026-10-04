export type CandidateMetric = 'close' | 'return_5d_pct' | 'return_20d_pct' | 'excess_5d_pp' | 'excess_20d_pp' | 'avg_turnover_20d' | 'turnover_ratio' | 'screen_score';

export type SelectionStatus = 'selected' | 'reserve' | 'excluded' | 'unverified';

export interface CandidateSelection {
  status: SelectionStatus;
  rank: number | null;
  score: string | null;
  reasons: string[];
}

export interface ScreeningSummary {
  policy_id: string;
  status: 'ready' | 'error';
  label: string;
  score_label: string;
  score_unit: string;
  criteria: string[];
  checked_at: string;
  master_observed_at: string | null;
  counts: Record<SelectionStatus, number>;
  error: string | null;
}

export interface CandidateRow {
  symbol: string;
  name: string;
  board: 'KOSPI' | 'KOSDAQ';
  status: 'ok' | 'excluded' | 'error';
  error: string | null;
  as_of: string | null;
  close: string | null;
  return_5d_pct: string | null;
  return_20d_pct: string | null;
  excess_5d_pp: string | null;
  excess_20d_pp: string | null;
  avg_turnover_20d: string | null;
  turnover_ratio: string | null;
  selection?: CandidateSelection;
}

export interface CandidateComparisonData {
  status: 'idle' | 'running' | 'complete' | 'error';
  error: string | null;
  updated_at: string | null;
  as_of: string | null;
  stale: boolean;
  progress: { completed: number; total: number };
  universe: {
    status: 'verified' | 'unverified';
    as_of: string | null;
    checked_at: string | null;
    source_url: string | null;
    count: number;
    error: string | null;
  };
  rows: CandidateRow[];
  screening?: ScreeningSummary;
}
