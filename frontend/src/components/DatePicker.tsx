import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import '../date-picker.css';

const DAY = 86_400_000;
const WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];

function parsed(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const day = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(day.getTime()) && day.toISOString().slice(0, 10) === value && day.getUTCFullYear() > 0 ? day : null;
}

function iso(day: Date): string { return day.toISOString().slice(0, 10); }
function monthStart(value: string): string { return `${value.slice(0, 7)}-01`; }
function plusDays(value: string, count: number): string { return iso(new Date(parsed(value)!.getTime() + count * DAY)); }
function plusMonths(value: string, count: number): string {
  const day = parsed(value)!;
  const first = parsed(monthStart(value))!;
  first.setUTCMonth(first.getUTCMonth() + count);
  const next = new Date(first);
  next.setUTCMonth(next.getUTCMonth() + 1);
  const lastDay = new Date(next.getTime() - DAY).getUTCDate();
  first.setUTCDate(Math.min(day.getUTCDate(), lastDay));
  return iso(first);
}

interface Props {
  id: string;
  label: string;
  value: string;
  max: string;
  onChange: (value: string) => void;
  invalid?: boolean;
  describedBy?: string;
}

export default function DatePicker({ id, label, value, max, onChange, invalid, describedBy }: Props) {
  const [open, setOpen] = useState(false);
  const [focusedDay, setFocusedDay] = useState(max);
  const [month, setMonth] = useState(monthStart(max));
  const [position, setPosition] = useState(() => ({ left: 12, top: 12, maxWidth: (document.documentElement.clientWidth || window.innerWidth) - 24 }));
  const field = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const grid = useRef<HTMLDivElement>(null);
  const focusDayOnRender = useRef(false);
  const popupId = `${id}-calendar`;
  const monthId = `${id}-month`;
  const monthDate = parsed(month)!;
  const firstCellTime = monthDate.getTime() - monthDate.getUTCDay() * DAY;
  const cells = Array.from({ length: 42 }, (_, index) => iso(new Date(firstCellTime + index * DAY)));
  const previousMonth = plusMonths(month, -1);
  const nextMonth = plusMonths(month, 1);

  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }
  function show() {
    const initial = parsed(value) && value <= max ? value : max;
    setFocusedDay(initial);
    setMonth(monthStart(initial));
    focusDayOnRender.current = true;
    setOpen(true);
    if (open && initial === focusedDay && month === monthStart(initial)) {
      grid.current?.querySelector<HTMLButtonElement>(`[data-date="${initial}"]`)?.focus({ preventScroll: true });
      focusDayOnRender.current = false;
    }
  }
  function moveFocus(next: string) {
    if (!parsed(next)) return;
    const allowed = next > max ? max : next;
    setFocusedDay(allowed);
    setMonth(monthStart(allowed));
    focusDayOnRender.current = true;
  }
  function choose(day: string) {
    if (day > max) return;
    onChange(day);
    close(true);
  }
  function calendarKey(event: KeyboardEvent<HTMLButtonElement>, day: string) {
    const weekday = parsed(day)!.getUTCDay();
    let next: string | undefined;
    if (event.key === 'ArrowLeft') next = plusDays(day, -1);
    else if (event.key === 'ArrowRight') next = plusDays(day, 1);
    else if (event.key === 'ArrowUp') next = plusDays(day, -7);
    else if (event.key === 'ArrowDown') next = plusDays(day, 7);
    else if (event.key === 'Home') next = plusDays(day, -weekday);
    else if (event.key === 'End') next = plusDays(day, 6 - weekday);
    else if (event.key === 'PageUp') next = plusMonths(day, event.shiftKey ? -12 : -1);
    else if (event.key === 'PageDown') next = plusMonths(day, event.shiftKey ? 12 : 1);
    if (next) { event.preventDefault(); moveFocus(next); }
  }
  function leaveCalendar(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'Tab') return;
    const selector = 'button:not(:disabled), input:not(:disabled), a[href], [tabindex]';
    const tabbable = (node: HTMLElement) => node.tabIndex >= 0 && !node.closest('[hidden]');
    const inside = Array.from(popup.current!.querySelectorAll<HTMLElement>(selector)).filter(tabbable);
    if (event.shiftKey && document.activeElement === inside[0]) {
      event.preventDefault();
      close(true);
    } else if (!event.shiftKey && document.activeElement === inside.at(-1)) {
      event.preventDefault();
      const outside = Array.from(document.querySelectorAll<HTMLElement>(selector)).filter(node => tabbable(node) && !popup.current?.contains(node));
      const next = outside[outside.indexOf(trigger.current!) + 1];
      close();
      (next ?? trigger.current)?.focus({ preventScroll: true });
    }
  }

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = field.current?.getBoundingClientRect();
      const panel = popup.current?.getBoundingClientRect();
      if (!anchor || !panel) return;
      const width = document.documentElement.clientWidth || window.innerWidth;
      const height = window.innerHeight;
      const panelWidth = Math.min(304, width - 24);
      const panelHeight = panel.height || 392;
      const below = anchor.bottom + 8;
      const top = below + panelHeight <= height - 12 ? below : Math.max(12, anchor.top - panelHeight - 8);
      setPosition({ left: Math.max(12, Math.min(anchor.left, width - panelWidth - 12)), top, maxWidth: width - 24 });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, { capture: true, passive: true });
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open]);

  useLayoutEffect(() => {
    if (open && focusDayOnRender.current) {
      grid.current?.querySelector<HTMLButtonElement>(`[data-date="${focusedDay}"]`)?.focus({ preventScroll: true });
      focusDayOnRender.current = false;
    }
  }, [open, focusedDay, month]);

  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      if (event.target instanceof Node && !field.current?.contains(event.target) && !popup.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); trigger.current?.focus({ preventScroll: true }); }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('focusin', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('focusin', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  return <div ref={field} className="date-picker-field">
    <label className="date-picker-label" htmlFor={id}>{label}</label>
    <div className="date-picker-control">
      <input id={id} className="date-picker-input" type="text" inputMode="numeric" autoComplete="off" spellCheck={false}
        placeholder="YYYY-MM-DD" maxLength={10} required max={max} value={value}
        aria-invalid={invalid || undefined} aria-describedby={[`${id}-hint`, describedBy].filter(Boolean).join(' ')}
        onChange={event => {
          const next = event.target.value;
          onChange(/^\d{8}$/.test(next) ? `${next.slice(0, 4)}-${next.slice(4, 6)}-${next.slice(6)}` : next);
        }} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); show(); } }}/>
      <button ref={trigger} type="button" className="date-picker-trigger" aria-label={`${label} 달력 열기`}
        aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? popupId : undefined} onClick={() => open ? close() : show()}>
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3.5" y="5.5" width="17" height="15" rx="3" stroke="currentColor" strokeWidth="1.6"/><path d="M7.5 3.5v4M16.5 3.5v4M3.5 10.5h17M8 14.5h2M14 14.5h2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"/></svg>
      </button>
    </div>
    <span id={`${id}-hint`} className="sr-only">연월일 8자리 또는 YYYY-MM-DD로 입력. 아래 화살표로 달력 열기.</span>
    {open ? createPortal(<div ref={popup} id={popupId} className="date-picker-popover" role="dialog" aria-label={`${label} 달력`}
      style={position} onKeyDown={leaveCalendar}>
      <div className="date-picker-month-nav">
        <button type="button" className="date-picker-month-button" aria-label="이전 달" disabled={!parsed(previousMonth)} onClick={() => {
          if (parsed(previousMonth)) { setMonth(previousMonth); setFocusedDay(previousMonth); }
        }}><span aria-hidden="true">‹</span></button>
        <h3 id={monthId} aria-live="polite">{monthDate.getUTCFullYear()}년 {monthDate.getUTCMonth() + 1}월</h3>
        <button type="button" className="date-picker-month-button" aria-label="다음 달" disabled={nextMonth > max}
          onClick={() => { setMonth(nextMonth); setFocusedDay(nextMonth); }}><span aria-hidden="true">›</span></button>
      </div>
      <div ref={grid} className="date-picker-grid" role="grid" aria-labelledby={monthId}>
        <div className="date-picker-week" role="row">{WEEKDAYS.map(day => <span role="columnheader" aria-label={`${day}요일`} key={day}>{day}</span>)}</div>
        {Array.from({ length: 6 }, (_, week) => <div className="date-picker-week" role="row" key={week}>
          {cells.slice(week * 7, week * 7 + 7).map(day => {
            const date = parsed(day);
            if (!date) return <div role="gridcell" key={day}/>;
            const selected = day === value;
            return <div role="gridcell" aria-selected={selected} key={day}>
              <button type="button" className={`date-picker-day${day.slice(0, 7) !== month.slice(0, 7) ? ' outside-month' : ''}${selected ? ' selected' : ''}`}
                data-date={day} disabled={day > max} tabIndex={day === focusedDay ? 0 : -1} aria-current={day === max ? 'date' : undefined}
                aria-label={`${date.getUTCFullYear()}년 ${date.getUTCMonth() + 1}월 ${date.getUTCDate()}일 ${WEEKDAYS[date.getUTCDay()]}요일`}
                onFocus={() => setFocusedDay(day)} onKeyDown={event => calendarKey(event, day)} onClick={() => choose(day)}>{date.getUTCDate()}</button>
            </div>;
          })}
        </div>)}
      </div>
      <div className="date-picker-footer"><button type="button" className="date-picker-today" onClick={() => choose(max)}>오늘</button>
        <button type="button" className="date-picker-close" onClick={() => close(true)}>닫기</button></div>
    </div>, document.body) : null}
  </div>;
}
