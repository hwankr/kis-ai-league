import { Fragment, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { number, numeric, timeFormatter } from '../format';
import type { HistoryData, HistoryPlaceholder } from '../types';

interface HistoryChartProps {
  history: HistoryData | null;
  accountId: string | null;
  accountName: string;
  placeholder?: HistoryPlaceholder;
}

const chartTimeFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Seoul', year: '2-digit', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});

function chartTime(date: Date, includeDate: boolean): string {
  const parts = Object.fromEntries(chartTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return includeDate ? `${parts.year}.${parts.month}.${parts.day} ${parts.hour}:${parts.minute}`
    : `${parts.hour}:${parts.minute}:${parts.second}`;
}

export default function HistoryChart({ history, accountId, accountName, placeholder }: HistoryChartProps) {
  const chart = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(240);
  const validHistory = history !== null && Array.isArray(history.points);
  const points = useMemo(() => (history && Array.isArray(history.points) ? history.points : []).map((point) => {
    const date = new Date(typeof point?.observed_at === 'string' ? point.observed_at : NaN);
    return { date, time: date.getTime(), value: numeric(point?.total_value) };
  }).filter((point) => Number.isFinite(point.time)).sort((a, b) => a.time - b.time), [history]);
  const valued = points.filter((point): point is typeof point & { value: number } => point.value !== null);
  const hasValues = valued.length > 0;

  useLayoutEffect(() => {
    const element = chart.current;
    if (!element) return;
    const resize = () => setWidth(Math.max(240, element.getBoundingClientRect().width));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasValues]);

  const totalCount = Math.max(points.length, numeric(history?.total_count) ?? points.length);
  const countLabel = points.length ? totalCount > points.length
    ? `최근 ${number(points.length)} / ${number(totalCount)}회` : `${number(totalCount)}회 조회` : '';
  const error = typeof history?.error === 'string' ? history.error : '';
  const state = !validHistory ? history === null ? placeholder?.state ?? 'loading' : 'error'
    : error ? 'error' : hasValues ? 'ready' : 'empty';
  const emptyMessage = !validHistory ? history === null ? placeholder?.message ?? '이력을 불러오는 중' : '이력을 불러오지 못했습니다'
    : points.length ? '총자산 값이 있는 이력이 없습니다' : error ? '이력을 표시할 수 없습니다' : '저장된 조회 이력이 없습니다';
  const first = points[0];
  const last = points.at(-1);
  const minimum = Math.min(...valued.map((point) => point.value));
  const maximum = Math.max(...valued.map((point) => point.value));
  const padding = (maximum - minimum) * 0.12 || Math.max(Math.abs(maximum) * 0.01, 1);
  const low = minimum - padding;
  const high = maximum + padding;
  const left = 94;
  const right = width - 12;
  const top = 16;
  const bottom = 184;
  const x = (time: number) => !first || !last || first.time === last.time ? (left + right) / 2
    : left + (time - first.time) / (last.time - first.time) * (right - left);
  const y = (value: number) => top + (high - value) / (high - low) * (bottom - top);
  const sameDay = first && last && chartTime(first.date, true).slice(0, 8) === chartTime(last.date, true).slice(0, 8);
  const axisLabel = (date: Date) => !sameDay && right - left < 240 ? chartTime(date, true).slice(0, 8) : chartTime(date, !sameDay);
  const segments: string[] = [];
  let segment: string[] = [];
  function finishSegment() {
    if (segment.length > 1) segments.push(segment.join(' '));
    segment = [];
  }
  for (const point of points) {
    // Unknown totals split the line; they are never zero or interpolated values.
    if (point.value === null) finishSegment();
    else segment.push(`${x(point.time)},${y(point.value)}`);
  }
  finishSegment();
  const svgVisibility = { hidden: !hasValues };

  return (
    <section id="asset-history" className="asset-history" aria-labelledby="history-title"
      data-state={state} data-account-id={accountId ?? ''} data-point-count={valued.length}
      data-observation-count={points.length} data-total-count={totalCount}>
      <div className="panel-heading">
        <div className="panel-title"><h2 id="history-title">자산 변화</h2><span id="history-count" className="history-count">{countLabel}</span></div>
        <span className="table-unit">원 · KST</span>
      </div>
      <p id="history-error" className="history-error" role="status" hidden={!error}>{error}</p>
      <figure className="history-figure">
        <svg id="history-chart" className="history-chart" role="img" ref={chart}
          aria-labelledby="history-chart-title history-chart-description" viewBox={`0 0 ${width} 220`} {...svgVisibility}>
          {hasValues && first && last && <>
            <title id="history-chart-title">{accountName || '선택 계좌'} 총자산 변화</title>
            <desc id="history-chart-description">{`${points.length}회 조회, 총자산 ${valued.length}개. ${timeFormatter.format(first.date)}부터 ${timeFormatter.format(last.date)}까지 KST. 최저 ${number(minimum)}원, 최고 ${number(maximum)}원.`}</desc>
            {[high, (high + low) / 2, low].map((value, index) => (
              <Fragment key={index}>
                <line x1={left} x2={right} y1={y(value)} y2={y(value)} className="history-grid" />
                <text x={left - 12} y={y(value) + 4} textAnchor="end" className="history-axis">{number(value)}</text>
              </Fragment>
            ))}
            {first.time === last.time
              ? <text x={x(first.time)} y={210} textAnchor="middle" className="history-axis">{chartTime(first.date, true)}</text>
              : <>
                <text x={left} y={210} textAnchor="start" className="history-axis">{axisLabel(first.date)}</text>
                <text x={right} y={210} textAnchor="end" className="history-axis">{axisLabel(last.date)}</text>
              </>}
            {segments.map((value, index) => <polyline key={index} points={value} className="history-line" />)}
            {valued.map((point, index) => (
              <circle key={`${point.time}-${index}`} cx={x(point.time)} cy={y(point.value)} r={valued.length === 1 ? 4 : 2.5}
                className="history-point" data-observed-at={point.date.toISOString()} data-value={point.value}>
                <title>{`${timeFormatter.format(point.date)} KST · ${number(point.value)}원`}</title>
              </circle>
            ))}
          </>}
        </svg>
        <figcaption id="history-latest" className="history-latest" hidden={!hasValues}>
          {hasValues && last ? `${timeFormatter.format(last.date)} KST · ${number(last.value)}원` : ''}
        </figcaption>
      </figure>
      <p id="history-empty" className="history-empty" hidden={hasValues}>{emptyMessage}</p>
    </section>
  );
}
