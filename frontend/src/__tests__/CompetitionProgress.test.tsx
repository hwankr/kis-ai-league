import { StrictMode } from 'react';
import { act, render, renderHook, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CompetitionProgressView } from '../components/CompetitionProgress';
import { readCompetitionProgress, type CompetitionProgressData } from '../competition-progress';
import useCompetitionProgress from '../useCompetitionProgress';
import { deferred, response } from './fixtures';

function fixture(): CompetitionProgressData {
  const metric = (value: number, target: number) => ({ value, target, met: value >= target, progress_pct: value / target * 100 });
  return {
    kis: { status: 'ok', phase: 'active', stale: false, error: null, updated_at: '2026-10-12T12:00:00+00:00',
      period: { start_date: '2026-10-12', end_date: '2026-11-20', as_of: '2026-10-12' }, coverage: 'complete',
      metrics: { trading_days: metric(1, 5), gross_amount: metric(4767100, 500000000), symbols: metric(7, 5),
        api_orders: { ...metric(7, 5), lower_bound: true } },
      pace: { calendar_basis: 'weekday_estimate', cutoff: '16:00_KST', status: 'behind',
        total_sessions: 30, completed_sessions: 1, remaining_sessions: 29,
        target_gross_amount: '500000000', gross_amount: '4767100', remaining_gross_amount: '495232900',
        expected_gross_amount: '16666666.67', shortfall_gross_amount: '11899566.67', required_daily_gross_amount: '17076996.56',
        next_5_sessions: 5, next_5_required_gross_amount: '85384982.76', recent_5_sessions: 1,
        recent_5_gross_amount: '4767100', projected_final_gross_amount: '143013000' } },
    timefolio: { status: 'ok', stale: false, error: null, updated_at: '2026-10-12T12:00:00+00:00',
      turnover: { week_start: '2026-10-12', week_end: '2026-10-16', current_pct: '2.3', target_pct: 5,
        status: 'below', missed_weeks: 0, max_missed_weeks: 3, source: 'official_guideline' } },
  };
}
const props = { loading: false, error: null, onRefresh: () => {} };

describe('competition progress', () => {
  it('shows five current/target metrics and confirmed missed weeks, caps only the bar', () => {
    render(<CompetitionProgressView {...props} data={readCompetitionProgress(fixture())}/>);
    expect(screen.getAllByRole('progressbar')).toHaveLength(5);
    expect(screen.getByText('4,767,100')).toBeTruthy();
    expect(screen.getByText('/ 5억원')).toBeTruthy();
    expect(screen.getByText('2.3')).toBeTruthy();
    expect(screen.getByText('확정 미달 0회 / 허용 3회')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: '거래 종목' }).getAttribute('value')).toBe('100');
    expect(screen.getByText('확인된 주문 기준')).toBeTruthy();
    expect(screen.getByRole('link', { name: '타임폴리오' }).getAttribute('href')).toBe('https://contest.timefolio.net/');
  });
  it('keeps official rounded 5.0 below target and distinguishes unknown from zero', () => {
    const data = fixture();
    data.timefolio.turnover!.current_pct = '5.0';
    const view = render(<CompetitionProgressView {...props} data={data}/>);
    const weekly = screen.getByRole('progressbar', { name: '주간 회전율' }).parentElement!;
    expect(within(weekly).queryByText('달성')).toBeNull();
    data.timefolio.turnover!.current_pct = null;
    data.timefolio.turnover!.status = 'unknown';
    data.timefolio.turnover!.missed_weeks = null;
    view.rerender(<CompetitionProgressView {...props} data={data}/>);
    expect(within(weekly).getByText('—')).toBeTruthy();
    expect(screen.getByText('공식 집계 대기')).toBeTruthy();
    expect(screen.getByText('확정 미달 —회 / 허용 3회')).toBeTruthy();
  });
  it('shows remaining gross and weekday pacing separately from Timefolio weekly turnover', () => {
    render(<CompetitionProgressView {...props} data={readCompetitionProgress(fixture())}/>);
    const pace = within(screen.getByRole('group', { name: '체결액 계획' }));
    expect(pace.getByText('체결액 계획 · 매수+매도')).toBeTruthy();
    expect(pace.getByText('누적 계획 미달')).toBeTruthy();
    expect(pace.getByText('495,232,900원')).toBeTruthy();
    expect(pace.getByText('남은 평일 · 추정')).toBeTruthy();
    expect(pace.getByText('29일')).toBeTruthy();
    expect(pace.getByText('17,076,996.56원')).toBeTruthy();
    expect(pace.getByText('다음 5평일 필요')).toBeTruthy();
    expect(pace.getByText('85,384,982.76원')).toBeTruthy();
    expect(pace.getByText('최근 1평일 일평균')).toBeTruthy();
    expect(pace.getByText('4,767,100원')).toBeTruthy();
  });
  it.each(['stale', 'partial', 'retained'] as const)('hides pacing claims for %s data', state => {
    const data = fixture();
    if (state === 'stale') data.kis.stale = true;
    if (state === 'partial') data.kis.coverage = 'partial';
    render(<CompetitionProgressView {...props} data={data} error={state === 'retained' ? '대회 요건 조회 실패' : null}/>);
    const pace = within(screen.getByRole('group', { name: '체결액 계획' }));
    expect(pace.getByText('집계 대기')).toBeTruthy();
    expect(pace.queryByText('누적 계획 미달')).toBeNull();
    expect(pace.queryByText('495,232,900원')).toBeNull();
    if (state !== 'partial') expect(pace.queryByText('29일')).toBeNull();
  });
  it('averages all completed weekdays and leaves an unobserved daily pace unknown', () => {
    const data = fixture(), pace = data.kis.pace!;
    Object.assign(pace, { completed_sessions: 5, remaining_sessions: 25, recent_5_sessions: 5, recent_5_gross_amount: '50000000' });
    const view = render(<CompetitionProgressView {...props} data={readCompetitionProgress(data)}/>);
    const panel = within(screen.getByRole('group', { name: '체결액 계획' }));
    expect(panel.getByText('최근 5평일 일평균').nextElementSibling?.textContent).toBe('10,000,000원');
    Object.assign(pace, { completed_sessions: 0, remaining_sessions: 30, recent_5_sessions: 0, recent_5_gross_amount: '0' });
    view.rerender(<CompetitionProgressView {...props} data={readCompetitionProgress(data)}/>);
    expect(panel.getByText('최근 0평일 일평균').nextElementSibling?.textContent).toBe('—');
  });
  it('shows deadline shortfall without a fictitious daily requirement', () => {
    const data = fixture(), pace = data.kis.pace!;
    Object.assign(pace, { status: 'ended_shortfall', completed_sessions: 30, remaining_sessions: 0,
      required_daily_gross_amount: null, next_5_sessions: 0, next_5_required_gross_amount: null, recent_5_sessions: 5 });
    render(<CompetitionProgressView {...props} data={readCompetitionProgress(data)}/>);
    const panel = within(screen.getByRole('group', { name: '체결액 계획' }));
    expect(panel.getByText('종료 · 목표 미달')).toBeTruthy();
    expect(panel.getByText('1일 필요').nextElementSibling?.textContent).toBe('—');
    expect(panel.getByText('다음 0평일 필요').nextElementSibling?.textContent).toBe('—');
  });
  it('marks retained data and preserves independent source errors', () => {
    const data = fixture();
    data.timefolio.error = '타임폴리오 로그인 필요';
    data.kis.stale = true;
    render(<CompetitionProgressView {...props} data={data} error="대회 요건 조회 실패"/>);
    expect(screen.getByRole('alert').textContent).toContain('이전 데이터');
    expect(screen.getByText('갱신 지연 · 이전 데이터')).toBeTruthy();
    expect(screen.getByText('타임폴리오 로그인 필요')).toBeTruthy();
    expect(screen.getByText('4,767,100')).toBeTruthy();
  });
  it('rejects nonfinite values, missing statuses and fabricated unknown achievements', () => {
    for (const change of [
      (d: CompetitionProgressData) => { d.kis.metrics.gross_amount.value = 'NaN'; },
      (d: CompetitionProgressData) => { d.kis.metrics.symbols.value = null; },
      (d: CompetitionProgressData) => { d.timefolio.turnover!.current_pct = null; d.timefolio.turnover!.status = 'met'; },
      (d: CompetitionProgressData) => { d.timefolio.turnover!.missed_weeks = -1; },
      (d: CompetitionProgressData) => { d.kis.pace!.required_daily_gross_amount = 'Infinity'; },
      (d: CompetitionProgressData) => { d.kis.pace!.remaining_sessions = 29.5; },
      (d: CompetitionProgressData) => { d.kis.pace!.recent_5_sessions = 5; },
      (d: CompetitionProgressData) => { d.kis.pace!.status = 'unavailable'; },
      (d: CompetitionProgressData) => { d.kis.pace!.gross_amount = null; },
    ]) {
      const data = fixture(); change(data);
      expect(() => readCompetitionProgress(data)).toThrow();
    }
  });
  it('ignores aborted StrictMode requests and clears old data when account data is unavailable', async () => {
    const old = deferred<Response>();
    const fetcher = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(response(fixture()));
    vi.stubGlobal('fetch', fetcher);
    const { result } = renderHook(useCompetitionProgress, { wrapper: StrictMode });
    await waitFor(() => expect(result.current.data?.kis.metrics.trading_days.value).toBe(1));
    const invalidated = fixture();
    for (const metric of Object.values(invalidated.kis.metrics)) { metric.value = null; metric.met = null; metric.progress_pct = null; }
    invalidated.kis.coverage = 'none';
    fetcher.mockResolvedValueOnce(response(invalidated));
    await act(async () => { await result.current.refresh(); });
    await act(async () => { old.resolve(response(fixture())); });
    expect(result.current.data?.kis.metrics.trading_days.value).toBeNull();
  });
});
