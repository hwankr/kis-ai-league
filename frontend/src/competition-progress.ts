import { numeric } from './format';
import type { Numeric } from './types';

export interface ProgressMetric { value: Numeric; target: Numeric; met: boolean | null; progress_pct: Numeric; lower_bound?: boolean }
export interface CompetitionPace {
  calendar_basis: 'weekday_estimate'; cutoff: '16:00_KST';
  status: 'not_started' | 'unavailable' | 'met' | 'behind' | 'on_track' | 'ended_shortfall';
  total_sessions: number; completed_sessions: number; remaining_sessions: number;
  target_gross_amount: Numeric; gross_amount: Numeric; remaining_gross_amount: Numeric;
  expected_gross_amount: Numeric; shortfall_gross_amount: Numeric; required_daily_gross_amount: Numeric;
  next_5_sessions: number; next_5_required_gross_amount: Numeric;
  recent_5_sessions: number; recent_5_gross_amount: Numeric; projected_final_gross_amount: Numeric;
}
export interface CompetitionProgressData {
  kis: {
    status: string; phase: 'not_started' | 'active' | 'ended'; stale: boolean; error: string | null; updated_at: string | null;
    period: { start_date: string; end_date: string; as_of: string };
    coverage: 'complete' | 'partial' | 'none';
    metrics: Record<'trading_days' | 'gross_amount' | 'symbols' | 'api_orders', ProgressMetric>;
    pace?: CompetitionPace;
  };
  timefolio: {
    status: string; stale: boolean; error: string | null; updated_at: string | null;
    turnover: { week_start: string | null; week_end: string | null; current_pct: Numeric; target_pct: number;
      status: 'met' | 'below' | 'exempt' | 'unknown'; missed_weeks: number | null; max_missed_weeks: number;
      source: string | null } | null;
  };
}
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const date = (v: unknown) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && Number.isFinite(Date.parse(v));
const instant = (v: unknown) => v === null || typeof v === 'string' && Number.isFinite(Date.parse(v));
const nonnegative = (v: unknown) => numeric(v) !== null && numeric(v)! >= 0;
const optionalNumber = (v: unknown) => v === null || nonnegative(v);
const optionalInteger = (v: unknown) => v === null || typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const error = () => new Error('대회 요건 응답 확인 필요');

export function readCompetitionProgress(value: unknown): CompetitionProgressData {
  if (!object(value) || !object(value.kis) || !object(value.timefolio)) throw error();
  const { kis, timefolio } = value;
  for (const source of [kis, timefolio]) {
    if (typeof source.status !== 'string' || typeof source.stale !== 'boolean'
      || !(source.error === null || typeof source.error === 'string') || !instant(source.updated_at)) throw error();
  }
  if (!['not_started', 'active', 'ended'].includes(String(kis.phase))
    || !['complete', 'partial', 'none'].includes(String(kis.coverage)) || !object(kis.period)
    || !date(kis.period.start_date) || !date(kis.period.end_date) || !date(kis.period.as_of) || !object(kis.metrics)) throw error();
  for (const key of ['trading_days', 'gross_amount', 'symbols', 'api_orders']) {
    const metric = kis.metrics[key];
    if (!object(metric) || !optionalNumber(metric.value) || !nonnegative(metric.target) || numeric(metric.target) === 0
      || !optionalNumber(metric.progress_pct) || !(metric.met === null || typeof metric.met === 'boolean')
      || metric.value === null && (metric.met !== null || metric.progress_pct !== null)
      || metric.lower_bound !== undefined && typeof metric.lower_bound !== 'boolean') throw error();
  }
  if (kis.pace !== undefined) {
    const p = kis.pace;
    if (!object(p) || p.calendar_basis !== 'weekday_estimate' || p.cutoff !== '16:00_KST'
      || !['not_started', 'unavailable', 'met', 'behind', 'on_track', 'ended_shortfall'].includes(String(p.status))
      || !nonnegative(p.target_gross_amount) || numeric(p.target_gross_amount) === 0) throw error();
    for (const key of ['total_sessions', 'completed_sessions', 'remaining_sessions', 'next_5_sessions', 'recent_5_sessions']) {
      if (p[key] === null || !optionalInteger(p[key])) throw error();
    }
    const total = p.total_sessions as number, completed = p.completed_sessions as number, remaining = p.remaining_sessions as number;
    if (total === 0 || completed + remaining !== total || (p.next_5_sessions as number) > Math.min(5, remaining)
      || (p.recent_5_sessions as number) > Math.min(5, completed)) throw error();
    for (const key of ['gross_amount', 'remaining_gross_amount', 'expected_gross_amount', 'shortfall_gross_amount',
      'required_daily_gross_amount', 'next_5_required_gross_amount', 'recent_5_gross_amount', 'projected_final_gross_amount']) {
      if (!optionalNumber(p[key]) || p.status === 'unavailable' && p[key] !== null) throw error();
    }
    if (p.status !== 'unavailable' && [p.gross_amount, p.remaining_gross_amount, p.expected_gross_amount,
      p.shortfall_gross_amount, p.recent_5_gross_amount].some(value => value === null)) throw error();
  }
  if (timefolio.turnover !== null) {
    const t = timefolio.turnover;
    if (!object(t) || !(t.week_start === null || date(t.week_start)) || !(t.week_end === null || date(t.week_end))
      || !optionalNumber(t.current_pct) || t.target_pct !== 5 || t.max_missed_weeks !== 3
      || !optionalInteger(t.missed_weeks) || !['met', 'below', 'exempt', 'unknown'].includes(String(t.status))
      || !(t.source === null || typeof t.source === 'string') || t.current_pct === null && ['met', 'below'].includes(String(t.status))) throw error();
  }
  return value as unknown as CompetitionProgressData;
}
