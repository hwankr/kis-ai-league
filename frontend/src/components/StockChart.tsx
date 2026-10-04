import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { number, timestamp } from '../format';
import type { ChartInterval, StockChartData } from '../types';
import type useStockChart from '../useStockChart';

const intervals: { value: ChartInterval; label: string }[] = [
  { value: 'day', label: '일봉' }, { value: '5m', label: '5분' }, { value: '15m', label: '15분' },
];
const intervalLabel = (value: ChartInterval) => intervals.find(interval => interval.value === value)!.label;
const barTime = (time: string) => time.length === 10 ? time : `${time.slice(0, 10)} ${time.slice(11, 16)}`;

function Candlesticks({ data }: { data: StockChartData }) {
  const chart = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(600);
  const [selectedTime, setSelectedTime] = useState<string | null>(null);
  const selectedIndex = selectedTime === null ? data.bars.length - 1 : Math.max(0, data.bars.findIndex(bar => bar.time === selectedTime));
  const selected = data.bars[selectedIndex];

  useLayoutEffect(() => {
    const element = chart.current;
    if (!element) return;
    const resize = () => setWidth(Math.max(240, element.getBoundingClientRect().width));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const minimum = Math.min(...data.bars.map(bar => Number(bar.low)));
  const maximum = Math.max(...data.bars.map(bar => Number(bar.high)));
  const padding = (maximum - minimum) * .08 || Math.max(maximum * .01, 1);
  const low = Math.max(0, minimum - padding);
  const high = maximum + padding;
  const left = 76;
  const right = width - 12;
  const top = 16;
  const bottom = 208;
  const volumeTop = 244;
  const volumeBottom = 304;
  const step = (right - left) / data.bars.length;
  const candleWidth = Math.max(1, Math.min(14, step * .66));
  const volumeMaximum = Math.max(1, ...data.bars.map(bar => Number(bar.volume)));
  const x = (index: number) => left + (index + .5) * step;
  const y = (price: number) => top + (high - price) / (high - low) * (bottom - top);
  const axisDate = (time: string) => data.interval === 'day' ? time.slice(5).replace('-', '.') : time.slice(11, 16);
  const selectAt = (clientX: number) => {
    const bounds = chart.current?.getBoundingClientRect();
    if (!bounds?.width) return;
    const position = (clientX - bounds.left) / bounds.width * width;
    const index = Math.max(0, Math.min(data.bars.length - 1, Math.floor((position - left) / step)));
    setSelectedTime(data.bars[index].time);
  };
  const selectedDescription = `${barTime(selected.time)} KST, 시가 ${number(selected.open)}원, 고가 ${number(selected.high)}원, 저가 ${number(selected.low)}원, 종가 ${number(selected.close)}원, 거래량 ${number(selected.volume)}주${selected.partial ? ', 진행 중' : ''}`;

  return <figure className="stock-chart-figure">
    <figcaption id="stock-chart-selection" className="stock-chart-selection">
      <div className="stock-chart-selected-time"><time dateTime={selected.time}>{barTime(selected.time)} KST</time>
        {selected.partial ? <span className="chart-partial">진행 중</span> : null}</div>
      <dl className="stock-chart-values">
        {([['시가', selected.open], ['고가', selected.high], ['저가', selected.low], ['종가', selected.close], ['거래량', selected.volume]] as const).map(([label, value]) =>
          <div key={label}><dt>{label}</dt><dd>{number(value)}<span>{label === '거래량' ? '주' : '원'}</span></dd></div>)}
      </dl>
    </figcaption>
    <svg id="stock-candles" ref={chart} className="stock-candles" viewBox={`0 0 ${width} 338`}
      tabIndex={0} role="slider" aria-label={`${data.name || data.symbol} ${intervalLabel(data.interval)} 차트`}
      aria-orientation="horizontal" aria-valuemin={1} aria-valuemax={data.bars.length} aria-valuenow={selectedIndex + 1}
      aria-valuetext={selectedDescription} aria-describedby="stock-chart-keys"
      onPointerDown={event => { selectAt(event.clientX); event.currentTarget.focus({ preventScroll: true }); }}
      onPointerMove={event => { if (event.pointerType !== 'touch' || event.buttons > 0) selectAt(event.clientX); }}
      onKeyDown={event => {
        const next = event.key === 'ArrowLeft' ? selectedIndex - 1 : event.key === 'ArrowRight' ? selectedIndex + 1
          : event.key === 'Home' ? 0 : event.key === 'End' ? data.bars.length - 1 : null;
        if (next === null) return;
        event.preventDefault();
        setSelectedTime(data.bars[Math.max(0, Math.min(data.bars.length - 1, next))].time);
      }}>
      <title>{data.name || data.symbol} {intervalLabel(data.interval)} 가격·거래량</title>
      {[high, (high + low) / 2, low].map((value, index) => <Fragment key={index}>
        <line x1={left} x2={right} y1={y(value)} y2={y(value)} className="history-grid" />
        <text x={left - 10} y={y(value) + 4} textAnchor="end" className="history-axis">{number(Math.round(value))}</text>
      </Fragment>)}
      <text x={left} y={232} className="history-axis">거래량</text>
      <line x1={left} x2={right} y1={volumeBottom} y2={volumeBottom} className="history-grid" />
      {data.bars.map((bar, index) => {
        const direction = Number(bar.close) > Number(bar.open) ? 'up' : Number(bar.close) < Number(bar.open) ? 'down' : 'flat';
        const volumeHeight = Number(bar.volume) / volumeMaximum * (volumeBottom - volumeTop);
        return <g key={bar.time} className={`stock-candle candle-${direction}${bar.partial ? ' candle-partial' : ''}`} data-time={bar.time}>
          <line x1={x(index)} x2={x(index)} y1={y(Number(bar.high))} y2={y(Number(bar.low))} />
          <rect x={x(index) - candleWidth / 2} y={Math.min(y(Number(bar.open)), y(Number(bar.close)))}
            width={candleWidth} height={Math.max(1, Math.abs(y(Number(bar.open)) - y(Number(bar.close))))} />
          <rect className="stock-volume-bar" x={x(index) - candleWidth / 2} y={volumeBottom - volumeHeight}
            width={candleWidth} height={volumeHeight} />
        </g>;
      })}
      <line x1={x(selectedIndex)} x2={x(selectedIndex)} y1={top} y2={volumeBottom} className="stock-chart-crosshair" />
      <circle cx={x(selectedIndex)} cy={y(Number(selected.close))} r={3.5} className="stock-chart-selected-point" />
      {data.bars.length === 1 ? <text x={x(0)} y={329} textAnchor="middle" className="history-axis">{axisDate(selected.time)}</text>
        : <>
          <text x={left} y={329} textAnchor="start" className="history-axis">{axisDate(data.bars[0].time)}</text>
          {width > 460 ? <text x={x(Math.floor(data.bars.length / 2))} y={329} textAnchor="middle" className="history-axis">{axisDate(data.bars[Math.floor(data.bars.length / 2)].time)}</text> : null}
          <text x={right} y={329} textAnchor="end" className="history-axis">{axisDate(data.bars.at(-1)!.time)}</text>
        </>}
    </svg>
    <span id="stock-chart-keys" className="sr-only">좌우 방향키로 봉 선택, Home으로 첫 봉, End로 마지막 봉</span>
  </figure>;
}

export default function StockChart({ chart }: { chart: ReturnType<typeof useStockChart> }) {
  const { data, request, interval, setInterval, loading, error, query, refresh } = chart;
  const [symbol, setSymbol] = useState('005930');
  const [inputError, setInputError] = useState<string | null>(null);
  useEffect(() => {
    if (request) { setSymbol(request.symbol); setInputError(null); }
  }, [request?.symbol]);
  const submit = (nextInterval = interval) => {
    const value = symbol.trim().toUpperCase();
    if (!/^[0-9A-Z]{6}$/.test(value)) { setInputError('영문·숫자 6자리 종목코드를 입력해 주세요.'); return; }
    setSymbol(value);
    setInputError(null);
    void query(value, nextInterval);
  };
  const updated = timestamp(data?.updated_at ?? null);
  const hasBars = Boolean(data?.bars.length);
  const stale = hasBars && Boolean(error || data?.stale);
  const status = loading ? hasBars ? '갱신 중' : '조회 중'
    : stale ? '이전 조회 데이터' : error ? '조회 실패' : hasBars ? `${number(data?.bars.length)}개 봉` : '';

  return <section id="stock-chart" className="stock-chart-panel" aria-labelledby="stock-chart-title" aria-busy={loading}
    data-symbol={request?.symbol ?? ''} data-interval={interval} data-bar-count={data?.bars.length ?? 0}>
    <div className="panel-heading">
      <div className="panel-title"><h2 id="stock-chart-title" tabIndex={-1}>종목 차트</h2></div>
      <span className={`chart-status${stale || error ? ' chart-status-warning' : ''}`} role="status">{status}</span>
    </div>
    <div className="stock-chart-controls">
      <form className="stock-chart-query" onSubmit={event => { event.preventDefault(); submit(); }}>
        <label className="sr-only" htmlFor="chart-symbol">차트 종목코드</label>
        <input id="chart-symbol" value={symbol} inputMode="text" maxLength={6} autoComplete="off" autoCapitalize="characters" spellCheck={false}
          aria-invalid={Boolean(inputError)} aria-describedby={inputError ? 'chart-input-error' : undefined}
          onChange={event => { setSymbol(event.target.value); setInputError(null); }} />
        <button type="submit" className="refresh-button chart-query-button" aria-label="종목 차트 조회">조회</button>
      </form>
      <div className="chart-intervals" role="group" aria-label="차트 주기">
        {intervals.map(option => <button key={option.value} type="button" aria-pressed={interval === option.value}
          onClick={() => {
            if (option.value === interval) return;
            if (request) submit(option.value); else setInterval(option.value);
          }}>{option.label}</button>)}
      </div>
      {request ? <button type="button" className="chart-refresh" disabled={loading} onClick={() => { setInputError(null); void refresh(); }}>차트 새로고침</button> : null}
    </div>
    {inputError ? <p id="chart-input-error" className="history-error" role="alert">{inputError}</p> : null}
    {error ? <p id="stock-chart-error" className="history-error" role="alert">{error}</p> : null}
    {data && hasBars ? <>
      <div className="stock-chart-identity"><h3>{data.name || data.symbol}</h3>{data.name ? <span>{data.symbol}</span> : null}
        <span>KRX · {intervalLabel(data.interval)}{data.adjusted ? ' · 수정주가' : ''}</span></div>
      <div className="stock-chart-meta"><span>{data.bars[0].time.slice(0, 10)}{data.bars[0].time.slice(0, 10) !== data.as_of ? ` ~ ${data.as_of}` : ''}</span>
        <span>마지막 거래일 <strong>{data.as_of}</strong></span></div>
      <Candlesticks key={`${data.symbol}-${data.interval}-${data.updated_at}`} data={data}/>
      <p className="stock-chart-source"><span>{data.source} · 원 / 주 · KST</span><span>조회 <time dateTime={updated.dateTime}>{updated.text}</time></span></p>
    </> : <p className="stock-chart-empty">{loading ? '차트를 불러오는 중' : error ? '차트를 표시할 수 없습니다' : request ? '조회 가능한 차트가 없습니다' : '종목을 선택하거나 코드를 입력해 조회하세요'}</p>}
  </section>;
}
