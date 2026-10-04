import { useMemo, useState } from 'react';
import type { CandidateComparisonData, CandidateMetric, CandidateRow, SelectionStatus } from '../candidates';
import { number, numeric, percent, signClass, timestamp } from '../format';
import StyledSelect from './StyledSelect';

interface Props {
  data: CandidateComparisonData | null;
  loading: boolean;
  error: string | null;
  onStart: () => void;
  onRefresh: () => void;
  onSelectSymbol: (symbol: string) => void;
}

const PAGE_SIZE = 25;
const EMPTY_ROWS: CandidateRow[] = [];
const selectionLabels: Record<SelectionStatus, string> = { selected: '관찰 후보', reserve: '순위 대기', excluded: '제외', unverified: '확인 필요' };
const boardOptions = [{ value: 'all', label: '전체 시장' }, { value: 'KOSPI', label: 'KOSPI' }, { value: 'KOSDAQ', label: 'KOSDAQ' }];
const selectionOptions = [{ value: 'all', label: '전체 종목' },
  ...(Object.keys(selectionLabels) as SelectionStatus[]).map(value => ({ value, label: selectionLabels[value] }))];
const metrics: { key: CandidateMetric; label: string; unit: string }[] = [
  { key: 'close', label: '수정종가', unit: '원' },
  { key: 'return_5d_pct', label: '5일 수익률', unit: '%' },
  { key: 'return_20d_pct', label: '20일 수익률', unit: '%' },
  { key: 'excess_5d_pp', label: '5일 지수 대비', unit: '%p' },
  { key: 'excess_20d_pp', label: '20일 지수 대비', unit: '%p' },
  { key: 'avg_turnover_20d', label: '20일 평균 거래대금', unit: '억원' },
  { key: 'turnover_ratio', label: '거래대금 배율', unit: '배' },
];

function metricNumber(row: CandidateRow, key: CandidateMetric): number | null {
  return numeric(key === 'screen_score' ? row.selection?.score : row[key]);
}

function metricValue(row: CandidateRow, key: CandidateMetric, unit = ''): string {
  const value = metricNumber(row, key);
  if (value === null) return '—';
  if (key === 'screen_score' && unit === '억원') return `${number(value)}억`;
  if (key === 'screen_score' && unit === '%p') return `${number(value, true)}%p`;
  if (key === 'return_5d_pct' || key === 'return_20d_pct') return percent(value);
  if (key === 'excess_5d_pp' || key === 'excess_20d_pp') return `${number(value, true)}%p`;
  if (key === 'avg_turnover_20d') return `${number(value / 100_000_000)}억`;
  if (key === 'turnover_ratio') return `${number(value)}배`;
  return number(value);
}

function sourceLink(source: string | null | undefined): string | null {
  if (!source) return null;
  try {
    const url = new URL(source);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export default function CandidateComparison({ data, loading, error, onStart, onRefresh, onSelectSymbol }: Props) {
  const [search, setSearch] = useState('');
  const [board, setBoard] = useState('all');
  const [selection, setSelection] = useState('selected');
  const [sort, setSort] = useState<{ key: CandidateMetric; direction: 'asc' | 'desc' }>({ key: 'screen_score', direction: 'desc' });
  const [page, setPage] = useState(1);
  const rows = data?.rows ?? EMPTY_ROWS;
  const screening = data?.screening;
  const unverifiedCount = rows.filter(row => (board === 'all' || row.board === board) && row.selection?.status === 'unverified').length;
  const sortKey = sort.key === 'screen_score' && !screening ? 'excess_20d_pp' : sort.key;
  const visibleMetrics = screening ? [{ key: 'screen_score' as const, label: screening.score_label, unit: screening.score_unit },
    ...metrics.filter(metric => metric.label !== screening.score_label || metric.unit !== screening.score_unit)] : metrics;
  const counts = useMemo(() => rows.reduce((total, row) => { total[row.status] += 1; return total; },
    { ok: 0, excluded: 0, error: 0 }), [rows]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase('ko-KR');
    return rows.filter(row => (board === 'all' || row.board === board)
      && (!screening || selection === 'all' || row.selection?.status === selection)
      && (!query || row.symbol.toLowerCase().includes(query) || row.name.toLocaleLowerCase('ko-KR').includes(query)))
      .sort((a, b) => {
        const left = metricNumber(a, sortKey);
        const right = metricNumber(b, sortKey);
        if (left === null && right !== null) return 1;
        if (right === null && left !== null) return -1;
        if (left !== null && right !== null && left !== right) return (left - right) * (sort.direction === 'asc' ? 1 : -1);
        if (sortKey === 'screen_score' && a.selection?.rank && b.selection?.rank) {
          return (a.selection.rank - b.selection.rank) * (sort.direction === 'desc' ? 1 : -1);
        }
        return a.symbol.localeCompare(b.symbol);
      });
  }, [rows, search, board, sort.direction, sortKey, screening, selection]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const running = data?.status === 'running';
  const warning = error || data?.error || data?.universe.error || screening?.error;
  const verified = data?.universe.status === 'verified';
  const source = sourceLink(data?.universe.source_url);
  const updated = timestamp(data?.updated_at ?? null);
  const status = error ? '상태 확인 필요' : running ? '비교 중' : data?.error ? counts.ok > 0 ? '일부 조회 실패' : '조회 실패' : data?.stale ? '이전 결과'
    : data?.status === 'complete' ? '비교 완료' : loading ? '상태 조회 중' : '비교 전';

  function changeSort(key: CandidateMetric, direction = sortKey === key && sort.direction === 'desc' ? 'asc' : 'desc') {
    setSort({ key, direction: direction as 'asc' | 'desc' });
    setPage(1);
  }

  return <section id="candidate-comparison" className="candidate-panel" aria-labelledby="candidate-title" aria-busy={loading}
    data-row-count={rows.length}>
    <div className="panel-heading candidate-heading">
      <div className="panel-title"><h2 id="candidate-title">후보 종목 비교</h2></div>
      <button className="refresh-button" type="button" disabled={loading || running || !verified} onClick={onStart}>
        {running ? '비교 중' : '전체 조회'}
      </button>
    </div>
    <div className="candidate-meta">
      <span>대회 대상 종목 전체</span>
      <span className={`candidate-state ${error || data?.error || data?.stale ? 'candidate-stale' : ''}`} role="status">
        {status}{data?.stale && rows.length > 0 && status !== '이전 결과' ? ' · 이전 결과' : ''}
        {running ? ` · ${number(data.progress.completed)} / ${number(data.progress.total)}` : ''}
      </span>
      {data?.as_of ? <span>기준일 <time dateTime={data.as_of}>{data.as_of}</time></span> : null}
      {data?.updated_at ? <span>갱신 <time dateTime={updated.dateTime}>{updated.text}</time></span> : null}
    </div>
    <div className="candidate-universe">
      <span>{verified ? `대상 목록 ${number(data.universe.count)}종목` : '대상 목록 미확인'}</span>
      {data?.universe.as_of ? <span>목록 기준 <time dateTime={data.universe.as_of}>{data.universe.as_of}</time></span> : null}
      {source ? <a href={source} target="_blank" rel="noreferrer">대상 목록 출처</a> : null}
    </div>
    {warning ? <div className="candidate-warning"><p className="history-error" role="alert">{warning}</p>
      <button type="button" className="candidate-retry" disabled={loading} onClick={onRefresh}>상태 다시 확인</button></div> : null}
    {data ? <p className="candidate-counts" aria-label="전체 비교 집계">
      {screening ? <strong>일봉 비교</strong> : null}
      <span>전체 {number(data.universe.count)}</span><span>성공 {number(counts.ok)}</span><span>제외 {number(counts.excluded)}</span><span>실패 {number(counts.error)}</span>
    </p> : null}
    {screening ? <div className="candidate-screening">
      <p className="candidate-counts" aria-label="후보 선별 집계">
        <strong>{screening.label}</strong>
        {(Object.keys(selectionLabels) as SelectionStatus[]).map(status => <span key={status}>{selectionLabels[status]} {number(screening.counts[status])}</span>)}
      </p>
      <details><summary>선별 기준</summary><ul>{screening.criteria.map(rule => <li key={rule}>{rule}</li>)}</ul>
        <p>상태 확인 <time dateTime={screening.checked_at}>{timestamp(screening.checked_at).text}</time></p>
      </details>
    </div> : null}
    {rows.length > 0 ? <>
      <div className="candidate-filters">
        <label className="candidate-search">종목 검색<input type="search" value={search} placeholder="종목명 또는 코드" onChange={event => { setSearch(event.target.value); setPage(1); }}/></label>
        <StyledSelect label="시장" value={board} options={boardOptions} onChange={value => { setBoard(value); setPage(1); }}/>
        {screening ? <StyledSelect label="선별 상태" value={selection} options={selectionOptions}
          onChange={value => { setSelection(value); setPage(1); }}/> : null}
        <StyledSelect label="정렬 지표" className="candidate-sort-select" value={sortKey}
          options={visibleMetrics.map(metric => ({ value: metric.key, label: metric.label }))}
          onChange={value => changeSort(value as CandidateMetric, sort.direction)}/>
        <button className="candidate-sort-direction" type="button" onClick={() => changeSort(sortKey)} aria-label={`정렬 방향: ${sort.direction === 'desc' ? '내림차순' : '오름차순'}`}>
          {sort.direction === 'desc' ? '높은 순 ↓' : '낮은 순 ↑'}
        </button>
      </div>
      {visibleRows.length > 0 ? <div className="table-scroll candidate-table-container" tabIndex={0} role="region" aria-label="후보 종목 비교 상세">
        <table className="candidate-table">
          <thead><tr><th scope="col">종목</th>{visibleMetrics.map(metric => <th scope="col" key={metric.key}
            aria-sort={sortKey === metric.key ? sort.direction === 'desc' ? 'descending' : 'ascending' : 'none'}>
            <button type="button" onClick={() => changeSort(metric.key)}>{metric.label}<span>({metric.unit}){sortKey === metric.key ? sort.direction === 'desc' ? ' ↓' : ' ↑' : ''}</span></button>
          </th>)}<th scope="col">기준일</th></tr></thead>
          <tbody>{visibleRows.map(row => <tr key={row.symbol} data-symbol={row.symbol}>
            <td className="candidate-stock"><button type="button" className="market-stock-link stock-name" aria-label={`${row.name || row.symbol} 차트 보기`} onClick={() => onSelectSymbol(row.symbol)}>{row.name || row.symbol}</button>
              <span className="stock-symbol">{row.symbol} · {row.board}</span>
              {screening && row.selection ? <span className={`candidate-selection candidate-selection-${row.selection.status}`}>
                {row.selection.rank !== null ? `${row.selection.rank} · ` : ''}
                {[...new Set([selectionLabels[row.selection.status], ...row.selection.reasons.flatMap(reason => reason.split(' · '))])].join(' · ')}
              </span> : null}
              {row.status !== 'ok' || row.error ? <span className="candidate-row-error">{row.status === 'excluded' ? '제외' : '실패'}{row.error ? ` · ${row.error}` : ''}</span> : null}
            </td>
            {visibleMetrics.map(metric => <td key={metric.key} className={`candidate-metric ${metric.key.includes('return') || metric.key.includes('excess') ? signClass(metricNumber(row, metric.key)) : ''}`}>
              <span className="candidate-mobile-label">{metric.label}</span><span>{metricValue(row, metric.key, metric.unit)}{metric.key === 'close' && row.close !== null ? <span className="candidate-mobile-label candidate-inline-unit">원</span> : null}</span>
            </td>)}
            <td className="candidate-date"><span className="candidate-mobile-label">기준일</span>{row.as_of ? <time dateTime={row.as_of}>{row.as_of}</time> : '—'}</td>
          </tr>)}</tbody>
        </table>
      </div> : <p className="market-empty">{screening && selection === 'selected' && !search
        ? unverifiedCount > 0
          ? `확인된 관찰 후보 없음 · 미확정 ${number(unverifiedCount)}종목`
          : '현재 조건을 통과한 관찰 후보 없음'
        : '검색 결과 없음'}</p>}
      <div className="candidate-pagination">
        <span role="status">{number(filtered.length)}종목 · {number(currentPage)} / {number(pageCount)}페이지</span>
        <nav aria-label="후보 종목 페이지"><button type="button" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>이전</button>
          <button type="button" disabled={currentPage >= pageCount} onClick={() => setPage(currentPage + 1)}>다음</button></nav>
      </div>
    </> : !warning ? <p className="market-empty">{loading ? '비교 상태 조회 중' : running ? '종목 데이터 조회 중' : '저장된 비교 결과 없음'}</p> : null}
  </section>;
}
