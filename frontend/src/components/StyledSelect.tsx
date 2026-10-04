import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import '../styled-select.css';

export interface SelectOption { value: string; label: string; disabled?: boolean }

interface Props {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
}

export default function StyledSelect({ label, value, options, onChange, disabled = false, className = '' }: Props) {
  const id = useId();
  const wrapper = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const search = useRef({ value: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const enabled = options.flatMap((option, index) => option.disabled ? [] : [index]);
  const unavailable = disabled || enabled.length === 0;
  const expanded = open && !unavailable;
  const selected = options.find(option => option.value === value);
  const signature = JSON.stringify(options);
  const active = expanded && enabled.includes(activeIndex) ? activeIndex : -1;
  const optionId = (index: number) => `${id}-option-${index}`;

  const close = useCallback(() => {
    setOpen(false);
    setActiveIndex(-1);
    search.current = { value: '', at: 0 };
  }, []);

  useEffect(() => { close(); }, [value, signature, disabled, close]);

  useEffect(() => {
    if (!expanded) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !wrapper.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', close);
    };
  }, [expanded, close]);

  useEffect(() => {
    if (active >= 0) list.current?.children[active]?.scrollIntoView?.({ block: 'nearest' });
  }, [active]);

  function show(fromEnd = false) {
    if (unavailable) return -1;
    const current = options.findIndex(option => option.value === value && !option.disabled);
    const next = current >= 0 ? current : (fromEnd ? enabled.at(-1) : enabled[0]) ?? -1;
    setActiveIndex(next);
    setOpen(true);
    return next;
  }

  function choose(index: number) {
    const option = options[index];
    if (unavailable || !option || option.disabled) return;
    close();
    trigger.current?.focus();
    if (option.value !== value) onChange(option.value);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (unavailable || event.nativeEvent.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const current = expanded ? active : show(event.key === 'ArrowUp' || event.key === 'End');
      let position = enabled.indexOf(current);
      if (event.key === 'Home') position = 0;
      else if (event.key === 'End') position = enabled.length - 1;
      else if (expanded) position = (position + (event.key === 'ArrowDown' ? 1 : -1) + enabled.length) % enabled.length;
      setActiveIndex(enabled[position]);
      search.current = { value: '', at: 0 };
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (expanded) choose(active);
      else show();
    } else if (event.key === 'Escape' && expanded) {
      event.preventDefault();
      close();
    } else if (event.key === 'Tab') {
      close();
    } else if (event.key.length === 1) {
      event.preventDefault();
      const current = expanded ? active : show();
      const now = Date.now();
      const text = (now - search.current.at < 700 ? search.current.value : '') + event.key.toLocaleLowerCase();
      const repeated = [...text].every(character => character === text[0]);
      const query = repeated ? text[0] : text;
      search.current = { value: query, at: now };
      const indices = repeated
        ? Array.from({ length: options.length }, (_, offset) => (current + offset + 1) % options.length)
        : enabled;
      const match = indices.find(index => !options[index].disabled && options[index].label.toLocaleLowerCase().startsWith(query));
      if (match !== undefined) setActiveIndex(match);
    }
  }

  return <div className={`styled-select-field ${className}`} ref={wrapper} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) close();
  }}>
    <label id={`${id}-label`} className="styled-select-label" htmlFor={`${id}-trigger`}>{label}</label>
    <div className="styled-select-control">
      <button id={`${id}-trigger`} ref={trigger} className="styled-select-trigger" type="button" role="combobox"
        aria-labelledby={`${id}-label`} aria-haspopup="listbox" aria-expanded={expanded}
        aria-controls={expanded ? `${id}-listbox` : undefined}
        aria-activedescendant={active >= 0 ? optionId(active) : undefined} disabled={unavailable}
        onClick={() => expanded ? close() : show()} onKeyDown={handleKeyDown}>
        <span>{selected?.label ?? '—'}</span>
        <svg className="styled-select-chevron" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="m6 8 4 4 4-4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {expanded ? <ul id={`${id}-listbox`} ref={list} className="styled-select-options" role="listbox"
        aria-labelledby={`${id}-label`} onPointerDown={event => event.preventDefault()}>
        {options.map((option, index) => <li key={option.value} id={optionId(index)} role="option"
          className={`styled-select-option${active === index ? ' is-active' : ''}`}
          aria-selected={option.value === value} aria-disabled={option.disabled || undefined}
          onClick={() => choose(index)} onPointerMove={() => { if (!option.disabled) setActiveIndex(index); }}>
          {option.label}
        </li>)}
      </ul> : null}
    </div>
  </div>;
}
