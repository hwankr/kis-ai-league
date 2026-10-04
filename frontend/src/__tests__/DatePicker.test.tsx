import { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import DatePicker from '../components/DatePicker';

function View({ initial = '2026-09-30', onChange = (_value: string) => {} }) {
  const [value, setValue] = useState(initial);
  return <><DatePicker id="start" label="시작일" value={value} max="2026-10-04" onChange={next => { setValue(next); onChange(next); }}/>
    <input aria-label="다음 입력"/><button type="button">다른 페이지</button></>;
}

describe('date picker', () => {
  it('preserves ISO direct entry, accepts eight digits, and selects a calendar date without submitting', async () => {
    const changed = vi.fn();
    render(<View onChange={changed}/>);
    const input = screen.getByLabelText('시작일') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '2026-09-12' } });
    expect(input.value).toBe('2026-09-12');
    fireEvent.change(input, { target: { value: '20260930' } });
    expect(input.value).toBe('2026-09-30');
    const trigger = screen.getByRole('button', { name: '시작일 달력 열기' });
    await userEvent.click(trigger);
    const calendar = screen.getByRole('dialog', { name: '시작일 달력' });
    expect(within(calendar).getAllByRole('columnheader')).toHaveLength(7);
    expect(within(calendar).getByRole('button', { name: '2026년 9월 30일 수요일' }).closest('[role="gridcell"]')?.getAttribute('aria-selected')).toBe('true');
    await userEvent.click(within(calendar).getByRole('button', { name: '2026년 9월 8일 화요일' }));
    expect(input.value).toBe('2026-09-08');
    expect(changed).toHaveBeenLastCalledWith('2026-09-08');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('moves months and keyboard focus across dates, limits future dates, and restores focus with Escape', async () => {
    render(<View/>);
    const trigger = screen.getByRole('button', { name: '시작일 달력 열기' });
    await userEvent.click(trigger);
    expect(document.activeElement?.getAttribute('data-date')).toBe('2026-09-30');
    screen.getByLabelText('시작일').focus();
    fireEvent.keyDown(screen.getByLabelText('시작일'), { key: 'ArrowDown' });
    expect(document.activeElement?.getAttribute('data-date')).toBe('2026-09-30');
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' });
    expect(document.activeElement?.getAttribute('data-date')).toBe('2026-10-01');
    expect(screen.getByRole('heading', { name: '2026년 10월' })).toBeTruthy();
    expect((screen.getByRole('button', { name: '2026년 10월 5일 월요일' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '다음 달' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
    expect(document.activeElement?.getAttribute('data-date')).toBe('2026-10-04');
    fireEvent.keyDown(document.activeElement!, { key: 'PageUp' });
    expect(document.activeElement?.getAttribute('data-date')).toBe('2026-09-04');
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('button', { name: '이전 달' }));
    expect(screen.getByRole('heading', { name: '2026년 8월' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '오늘' }));
    expect((screen.getByLabelText('시작일') as HTMLInputElement).value).toBe('2026-10-04');
  });

  it('closes on pointer/focus outside and returns Tab to the next logical field outside the portal', async () => {
    render(<View/>);
    const trigger = screen.getByRole('button', { name: '시작일 달력 열기' });
    await userEvent.click(trigger);
    screen.getByRole('button', { name: '닫기' }).focus();
    await userEvent.tab();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(screen.getByLabelText('다음 입력'));
    await userEvent.click(trigger);
    screen.getByRole('button', { name: '이전 달' }).focus();
    await userEvent.tab({ shift: true });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('button', { name: '다른 페이지' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(trigger);
    fireEvent.focusIn(screen.getByLabelText('다음 입력'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('handles the minimum year without invalid adjacent cells or keyboard crashes', async () => {
    render(<View initial="0001-01-01"/>);
    await userEvent.click(screen.getByRole('button', { name: '시작일 달력 열기' }));
    expect(screen.getByRole('heading', { name: '1년 1월' })).toBeTruthy();
    expect((screen.getByRole('button', { name: '이전 달' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    fireEvent.keyDown(document.activeElement!, { key: 'PageUp' });
    expect(document.activeElement?.getAttribute('data-date')).toBe('0001-01-01');
  });

  it('bounds the popover to the measured viewport including scrollbar gutter', async () => {
    const width = vi.spyOn(document.documentElement, 'clientWidth', 'get').mockReturnValue(305);
    render(<View/>);
    await userEvent.click(screen.getByRole('button', { name: '시작일 달력 열기' }));
    expect(screen.getByRole('dialog').style.maxWidth).toBe('281px');
    width.mockRestore();
  });
});
