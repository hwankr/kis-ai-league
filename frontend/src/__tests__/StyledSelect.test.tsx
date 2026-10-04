import { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import StyledSelect from '../components/StyledSelect';

const options = [
  { value: 'alpha', label: 'Alpha' },
  { value: 'pending', label: 'Pending', disabled: true },
  { value: 'beta', label: 'Beta' },
  { value: 'bravo', label: 'Bravo' },
];

function Harness({ onChange }: { onChange: (value: string) => void }) {
  const [value, setValue] = useState('alpha');
  return <StyledSelect label="기준" value={value} options={options} onChange={next => {
    setValue(next);
    onChange(next);
  }} />;
}

function expectActive(trigger: HTMLElement, name: string) {
  const option = screen.getByRole('option', { name });
  expect(option.id).not.toBe('');
  expect(trigger.getAttribute('aria-activedescendant')).toBe(option.id);
  expect(document.activeElement).toBe(trigger);
}

describe('styled select', () => {
  it('keeps focus on the trigger, skips disabled options and commits with Enter or Space', async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByRole('combobox', { name: /기준/ });
    trigger.focus();
    await userEvent.keyboard('{ArrowDown}');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(trigger.getAttribute('aria-controls')).toBe(screen.getByRole('listbox').id);
    expectActive(trigger, 'Alpha');
    expect(screen.getByRole('option', { name: 'Alpha' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('option', { name: 'Pending' }).getAttribute('aria-disabled')).toBe('true');

    await userEvent.keyboard('{ArrowDown}');
    expectActive(trigger, 'Beta');
    expect(screen.getByRole('option', { name: 'Beta' }).getAttribute('aria-selected')).toBe('false');
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledExactlyOnceWith('beta');
    expect(trigger.textContent).toContain('Beta');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(document.activeElement).toBe(trigger);

    await userEvent.keyboard(' ');
    expectActive(trigger, 'Beta');
    await userEvent.keyboard('{Home} ');
    expect(onChange.mock.calls).toEqual([['beta'], ['alpha']]);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);
  });

  it('wraps keyboard navigation and cancels with Escape or Tab without committing', async () => {
    const onChange = vi.fn();
    render(<><Harness onChange={onChange} /><button>다음</button></>);
    const trigger = screen.getByRole('combobox');
    trigger.focus();
    await userEvent.keyboard('{ArrowDown}{ArrowUp}');
    expectActive(trigger, 'Bravo');
    await userEvent.keyboard('{ArrowDown}');
    expectActive(trigger, 'Alpha');
    await userEvent.keyboard('{End}');
    expectActive(trigger, 'Bravo');
    await userEvent.keyboard('{Home}');
    expectActive(trigger, 'Alpha');
    await userEvent.keyboard('{ArrowDown}{Escape}');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(document.activeElement).toBe(trigger);
    await userEvent.keyboard('{ArrowDown}{End}');
    await userEvent.tab();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '다음' }));
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger.textContent).toContain('Alpha');
  });

  it('matches typed prefixes, cycles repeated letters and resets the prefix after 700 ms', () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByRole('combobox');
    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    fireEvent.keyDown(trigger, { key: 'b' });
    expectActive(trigger, 'Beta');
    fireEvent.keyDown(trigger, { key: 'r' });
    expectActive(trigger, 'Bravo');
    act(() => vi.advanceTimersByTime(701));
    fireEvent.keyDown(trigger, { key: 'b' });
    expectActive(trigger, 'Beta');
    fireEvent.keyDown(trigger, { key: 'b' });
    expectActive(trigger, 'Bravo');
    fireEvent.keyDown(trigger, { key: 'b' });
    expectActive(trigger, 'Beta');
    act(() => vi.advanceTimersByTime(701));
    fireEvent.keyDown(trigger, { key: 'a' });
    expectActive(trigger, 'Alpha');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('ignores a disabled option and retains trigger focus after a pointer selection', async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const trigger = screen.getByRole('combobox');
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole('option', { name: 'Pending' }));
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    await userEvent.click(screen.getByRole('option', { name: 'Bravo' }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith('bravo');
    expect(trigger.textContent).toContain('Bravo');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);
  });

  it('closes on an outside pointer without committing or stealing outside focus', async () => {
    const onChange = vi.fn();
    render(<><Harness onChange={onChange} /><button>외부</button></>);
    const trigger = screen.getByRole('combobox');
    await userEvent.click(trigger);
    await userEvent.keyboard('{End}');
    const outside = screen.getByRole('button', { name: '외부' });
    await userEvent.click(outside);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(document.activeElement).toBe(outside);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('closes and disables the trigger when disabled or when its options become empty', async () => {
    const props = { label: '기준', value: 'alpha', onChange: vi.fn() };
    const view = render(<StyledSelect {...props} options={options} />);
    const trigger = screen.getByRole('combobox') as HTMLButtonElement;
    await userEvent.click(trigger);
    view.rerender(<StyledSelect {...props} options={options} disabled />);
    expect(trigger.disabled).toBe(true);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    await userEvent.click(trigger);
    expect(screen.queryByRole('listbox')).toBeNull();

    view.rerender(<StyledSelect {...props} options={options} />);
    await userEvent.click(trigger);
    view.rerender(<StyledSelect {...props} options={[]} />);
    expect(trigger.disabled).toBe(true);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(props.onChange).not.toHaveBeenCalled();
  });

  it('closes safely when controlled values or options change without committing a removed option', async () => {
    const onChange = vi.fn();
    const props = { label: '기준', options, onChange };
    const view = render(<StyledSelect {...props} value="alpha" />);
    const trigger = screen.getByRole('combobox');
    await userEvent.click(trigger);
    view.rerender(<StyledSelect {...props} value="beta" />);
    expect(trigger.textContent).toContain('Beta');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.keyboard('{ArrowDown}');
    expect(screen.getByRole('option', { name: 'Beta' }).getAttribute('aria-selected')).toBe('true');
    expectActive(trigger, 'Beta');
    await userEvent.keyboard('{End}');
    expectActive(trigger, 'Bravo');
    view.rerender(<StyledSelect {...props} value="beta" options={[options[0], options[2]]} />);
    expect(screen.queryByRole('option', { name: 'Bravo' })).toBeNull();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
    expect(onChange).not.toHaveBeenCalled();
    await userEvent.keyboard('{ArrowDown}');
    expectActive(trigger, 'Beta');
    await userEvent.keyboard('{Home}{Enter}');
    expect(onChange).toHaveBeenCalledExactlyOnceWith('alpha');
  });

  it('uses distinct listbox and option IDs across multiple instances', async () => {
    render(<>
      <StyledSelect label="시장" value="alpha" options={options} onChange={vi.fn()} />
      <StyledSelect label="범위" value="alpha" options={options} onChange={vi.fn()} />
    </>);
    const first = screen.getByRole('combobox', { name: /시장/ });
    const second = screen.getByRole('combobox', { name: /범위/ });
    await userEvent.click(first);
    const firstListId = screen.getByRole('listbox').id;
    const firstOptionIds = screen.getAllByRole('option').map(option => option.id);
    expect(firstListId).not.toBe('');
    expect(first.getAttribute('aria-controls')).toBe(firstListId);
    await userEvent.click(second);
    const secondListId = screen.getByRole('listbox').id;
    const secondOptionIds = screen.getAllByRole('option').map(option => option.id);
    expect(second.getAttribute('aria-controls')).toBe(secondListId);
    expect(secondListId).not.toBe(firstListId);
    const ids = [firstListId, secondListId, ...firstOptionIds, ...secondOptionIds];
    expect(ids.every(Boolean)).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
    expectActive(second, 'Alpha');
  });
});
