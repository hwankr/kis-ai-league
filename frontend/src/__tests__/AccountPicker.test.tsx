import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import AccountPicker from '../components/AccountPicker';
import { element } from './fixtures';

const choices = [
  { id: 'alpha', name: 'Alpha', configured: true },
  { id: 'pending', name: 'Pending', configured: false },
  { id: 'beta', name: 'Beta', configured: true },
];

describe('account picker', () => {
  it('supports keyboard navigation, skips unconfigured accounts, and returns focus after selection', async () => {
    const onSelect = vi.fn();
    render(<AccountPicker accounts={choices} selectedId="alpha" loading={false} onSelect={onSelect} />);
    const trigger = screen.getByRole('combobox');
    trigger.focus();
    await userEvent.keyboard('{ArrowDown}');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    await userEvent.keyboard('{ArrowDown}');
    expect(trigger.getAttribute('aria-activedescendant')).toBe(screen.getByRole('option', { name: 'Beta' }).id);
    await userEvent.keyboard('{Enter}');
    expect(onSelect).toHaveBeenCalledExactlyOnceWith('beta');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false);
  });

  it('supports first/last navigation, typeahead, Escape and Tab without changing the selection', async () => {
    const onSelect = vi.fn();
    render(<AccountPicker accounts={choices} selectedId="alpha" loading={false} onSelect={onSelect} />);
    const trigger = screen.getByRole('combobox');
    trigger.focus();
    await userEvent.keyboard('{End}');
    expect(trigger.getAttribute('aria-activedescendant')).toBe(screen.getByRole('option', { name: 'Beta' }).id);
    await userEvent.keyboard('{Home}');
    expect(trigger.getAttribute('aria-activedescendant')).toBe(screen.getByRole('option', { name: 'Alpha' }).id);
    await userEvent.keyboard('b');
    expect(trigger.getAttribute('aria-activedescendant')).toBe(screen.getByRole('option', { name: 'Beta' }).id);
    await userEvent.keyboard('{Escape}');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    await userEvent.keyboard(' ');
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    await userEvent.tab();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('does not choose an unconfigured account and closes when clicking outside', async () => {
    const onSelect = vi.fn();
    render(<><AccountPicker accounts={choices} selectedId="alpha" loading={false} onSelect={onSelect} /><button>외부</button></>);
    await userEvent.click(screen.getByRole('combobox'));
    const pending = screen.getByRole('option', { name: /Pending\s*설정 필요/ });
    expect(pending.getAttribute('aria-disabled')).toBe('true');
    await userEvent.click(pending);
    expect(onSelect).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '외부' }));
    expect(element('account-select').getAttribute('aria-expanded')).toBe('false');
  });

  it('disables an empty catalog and closes an open list when resized', async () => {
    const props = { selectedId: null, loading: false, onSelect: vi.fn() };
    const view = render(<AccountPicker {...props} accounts={[]} />);
    expect((screen.getByRole('combobox') as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<AccountPicker {...props} accounts={choices} />);
    await userEvent.click(screen.getByRole('combobox'));
    fireEvent(window, new Event('resize'));
    expect(element('account-select').getAttribute('aria-expanded')).toBe('false');
  });
});
