import { useCallback, useEffect, useRef, useState } from 'react';
import type { CandidateComparisonData, CandidateRow } from './candidates';
import { numeric } from './format';

const LOAD_ERROR = '후보 종목 비교 상태를 불러오지 못했습니다.';

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function nullableString(value: unknown): boolean { return value === null || typeof value === 'string'; }
function date(value: unknown): boolean {
  return value === null || typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
}
function time(value: unknown): boolean { return value === null || typeof value === 'string' && Number.isFinite(Date.parse(value)); }
function count(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0; }

function readComparison(value: unknown): CandidateComparisonData {
  const data = record(value);
  const progress = record(data?.progress);
  const universe = record(data?.universe);
  if (!data || !['idle', 'running', 'complete', 'error'].includes(String(data.status)) || !nullableString(data.error)
    || !time(data.updated_at) || !date(data.as_of) || typeof data.stale !== 'boolean'
    || !progress || !count(progress.completed) || !count(progress.total) || progress.completed > progress.total
    || !universe || !['verified', 'unverified'].includes(String(universe.status)) || !date(universe.as_of)
    || !time(universe.checked_at) || !nullableString(universe.source_url) || !count(universe.count)
    || !nullableString(universe.error) || !Array.isArray(data.rows)) throw new Error('Invalid candidate response');
  const symbols = new Set<string>();
  const screening = data.screening === undefined ? undefined : record(data.screening);
  if (screening !== undefined) {
    const counts = record(screening?.counts);
    if (!screening || !['ready', 'error'].includes(String(screening.status))
      || ['policy_id', 'label', 'score_label', 'score_unit'].some(key => typeof screening[key] !== 'string')
      || !Array.isArray(screening.criteria) || screening.criteria.some(value => typeof value !== 'string')
      || typeof screening.checked_at !== 'string' || !time(screening.checked_at)
      || !time(screening.master_observed_at) || !nullableString(screening.error)
      || !counts || ['selected', 'reserve', 'excluded', 'unverified'].some(key => !count(counts[key]))) {
      throw new Error('Invalid screening response');
    }
  }
  const rows = data.rows.map((value: unknown): CandidateRow => {
    const row = record(value);
    if (!row || typeof row.symbol !== 'string' || !/^[0-9A-Z]{6}$/.test(row.symbol) || symbols.has(row.symbol)
      || typeof row.name !== 'string' || !['KOSPI', 'KOSDAQ'].includes(String(row.board))
      || !['ok', 'excluded', 'error'].includes(String(row.status)) || !nullableString(row.error) || !date(row.as_of)
      || ['close', 'return_5d_pct', 'return_20d_pct', 'excess_5d_pp', 'excess_20d_pp', 'avg_turnover_20d', 'turnover_ratio']
        .some(key => row[key] !== null && (typeof row[key] !== 'string' || numeric(row[key]) === null))
      || ['close', 'avg_turnover_20d', 'turnover_ratio'].some(key => row[key] !== null && numeric(row[key])! < 0)) {
      throw new Error('Invalid candidate row');
    }
    symbols.add(row.symbol);
    if (screening) {
      const selection = record(row.selection);
      if (!selection || !['selected', 'reserve', 'excluded', 'unverified'].includes(String(selection.status))
        || selection.rank !== null && (!count(selection.rank) || selection.rank < 1)
        || selection.score !== null && (typeof selection.score !== 'string' || numeric(selection.score) === null)
        || !Array.isArray(selection.reasons) || selection.reasons.some(value => typeof value !== 'string')) {
        throw new Error('Invalid screening row');
      }
    }
    return row as unknown as CandidateRow;
  });
  if (screening) {
    const counts = screening.counts as Record<string, number>;
    for (const status of ['selected', 'reserve', 'excluded', 'unverified']) {
      if (counts[status] !== rows.filter(row => row.selection?.status === status).length) throw new Error('Invalid screening counts');
    }
  }
  return { ...data, rows } as unknown as CandidateComparisonData;
}

export default function useCandidateComparison() {
  const [data, setData] = useState<CandidateComparisonData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const session = useRef({ mounted: false, generation: 0, controller: null as AbortController | null,
    timeout: undefined as number | undefined, status: 'idle' as CandidateComparisonData['status'], verified: false });

  const request = useCallback(async (start = false) => {
    const current = session.current;
    if (!current.mounted || current.controller || start && (!current.verified || current.status === 'running')) return;
    const controller = new AbortController();
    const generation = ++current.generation;
    current.controller = controller;
    setLoading(true);
    const isCurrent = () => current.mounted && current.generation === generation;
    const timeout = window.setTimeout(() => controller.abort(), 25_000);
    current.timeout = timeout;
    try {
      const response = await fetch('/api/candidates', {
        method: start ? 'POST' : 'GET',
        headers: { Accept: 'application/json', 'X-KIS-Dashboard': '1', ...(start ? { 'Content-Type': 'application/json' } : {}) },
        ...(start ? { body: '{}' } : {}), cache: 'no-store', signal: controller.signal,
      });
      const payload: unknown = await response.json();
      if (!isCurrent()) return;
      if (controller.signal.aborted) throw new Error('Request timed out');
      const comparison = readComparison(payload);
      current.status = comparison.status;
      current.verified = comparison.universe.status === 'verified';
      const failed = !response.ok || comparison.status === 'error';
      setData(previous => {
        if (!comparison.rows.length && (failed || comparison.status === 'running') && previous?.rows.length) {
          return { ...comparison, stale: true, rows: previous.rows, screening: previous.screening, as_of: previous.as_of, updated_at: previous.updated_at };
        }
        return { ...comparison, stale: failed || comparison.stale };
      });
      setError(failed ? comparison.error || LOAD_ERROR : null);
    } catch {
      if (isCurrent()) {
        setError(LOAD_ERROR);
        setData(previous => previous ? { ...previous, stale: true } : previous);
      }
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
    void request();
    return () => {
      current.mounted = false;
      ++current.generation;
      current.controller?.abort();
      window.clearTimeout(current.timeout);
      current.controller = null;
      current.timeout = undefined;
    };
  }, [request]);

  useEffect(() => {
    if (data?.status !== 'running' || loading) return;
    const timer = window.setTimeout(() => { void request(); }, 2_000);
    return () => window.clearTimeout(timer);
  }, [data, loading, request]);

  const start = useCallback(() => request(true), [request]);
  const refresh = useCallback(() => request(), [request]);
  return { data, loading, error, start, refresh };
}
