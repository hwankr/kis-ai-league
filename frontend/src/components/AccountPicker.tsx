import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { AccountChoice } from '../types';

interface AccountPickerProps {
  accounts: AccountChoice[];
  selectedId: string | null;
  loading: boolean;
  emptyLabel?: string;
  onSelect: (id: string) => void;
}

export default function AccountPicker({ accounts, selectedId, loading, emptyLabel, onSelect }: AccountPickerProps) {
  const wrapper = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const options = useRef<HTMLUListElement>(null);
  const search = useRef({ value: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [fitWidth, setFitWidth] = useState(false);
  const selected = accounts.find((account) => account.id === selectedId);
  const enabled = accounts.flatMap((account, index) => account.configured ? [index] : []);

  const close = useCallback(() => {
    setOpen(false);
    setActiveIndex(-1);
    search.current = { value: '', at: 0 };
  }, []);

  useEffect(() => { close(); }, [accounts, selectedId, close]);

  useEffect(() => {
    const outsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !wrapper.current?.contains(event.target)) close();
    };
    document.addEventListener('pointerdown', outsidePointer);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', outsidePointer);
      window.removeEventListener('resize', close);
    };
  }, [close]);

  useEffect(() => {
    if (open && activeIndex >= 0) {
      options.current?.children[activeIndex]?.scrollIntoView?.({ block: 'nearest' });
    }
  }, [open, activeIndex]);

  function show(fromEnd = false): number {
    if (!accounts.length) return -1;
    const bounds = wrapper.current?.getBoundingClientRect();
    if (bounds) {
      const available = document.documentElement.clientWidth - bounds.left - 16;
      setFitWidth(Math.min(360, Math.max(bounds.width, 228)) > available);
    }
    const selectedIndex = accounts.findIndex((account) => account.id === selectedId && account.configured);
    const nextIndex = selectedIndex >= 0 ? selectedIndex : (fromEnd ? enabled.at(-1) : enabled[0]) ?? -1;
    setOpen(true);
    setActiveIndex(nextIndex);
    return nextIndex;
  }

  function choose(index: number) {
    const account = accounts[index];
    if (!account?.configured) return;
    close();
    trigger.current?.focus();
    if (account.id !== selectedId) onSelect(account.id);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.nativeEvent.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const currentIndex = open ? activeIndex : show(event.key === 'ArrowUp' || event.key === 'End');
      if (!enabled.length) return;
      let position = enabled.indexOf(currentIndex);
      if (event.key === 'Home') position = 0;
      else if (event.key === 'End') position = enabled.length - 1;
      else if (open) position = (position + (event.key === 'ArrowDown' ? 1 : -1) + enabled.length) % enabled.length;
      setActiveIndex(enabled[position]);
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (open) choose(activeIndex);
      else show();
    } else if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
    } else if (event.key === 'Tab') {
      close();
    } else if (event.key.length === 1) {
      event.preventDefault();
      if (!open) show();
      const now = Date.now();
      const value = (now - search.current.at < 700 ? search.current.value : '') + event.key.toLocaleLowerCase();
      search.current = { value, at: now };
      const match = accounts.findIndex((account) => account.configured && account.name.toLocaleLowerCase().startsWith(value));
      if (match >= 0) setActiveIndex(match);
    }
  }

  return (
    <div className="account-picker">
      <span className="account-broker">한국투자증권</span>
      <div className="account-select-wrap" ref={wrapper} onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) close();
      }}>
        <span id="account-select-label" className="sr-only">조회할 계좌</span>
        <button id="account-select" className="account-select-trigger" type="button" role="combobox"
          ref={trigger} aria-labelledby="account-select-label account-select-value" aria-haspopup="listbox"
          aria-expanded={open} aria-controls="account-options"
          aria-activedescendant={open && activeIndex >= 0 ? `account-option-${activeIndex}` : undefined}
          disabled={!accounts.length} onClick={() => open ? close() : show()} onKeyDown={handleKeyDown}>
          <span id="account-select-value">{selected?.name || (accounts.length ? '계좌 선택' : emptyLabel ?? (loading ? '계좌 목록을 불러오는 중' : '등록된 계좌 없음'))}</span>
          <svg className="account-select-chevron" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path d="m6 8 4 4 4-4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        <ul id="account-options" className={`account-options${fitWidth ? ' account-options-fit' : ''}`}
          ref={options} role="listbox" aria-labelledby="account-select-label" hidden={!open}
          onPointerDown={(event) => event.preventDefault()}>
          {accounts.map((account, index) => (
            <li key={account.id} id={`account-option-${index}`} data-index={index}
              className={`account-option${open && activeIndex === index ? ' is-active' : ''}`}
              role="option" aria-selected={account.id === selectedId} aria-disabled={!account.configured}
              onClick={() => choose(index)} onPointerMove={() => {
                if (account.configured) setActiveIndex(index);
              }}>
              <span className="account-option-name">{account.name}</span>
              {!account.configured && <span className="account-option-meta">설정 필요</span>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
