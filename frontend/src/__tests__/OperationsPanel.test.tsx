import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import OperationsPanel, { OperationsStatus } from '../components/OperationsPanel';
import { readOperations } from '../operations';
import type { OperationsData } from '../operations';
import useOperations from '../useOperations';
import { deferred, response } from './fixtures';

function operations(overrides: Partial<OperationsData> = {}): OperationsData {
  return { dashboard: { status: 'ok', updated_at: '2026-10-10T01:00:00Z' },
    mirror: { status: 'stopped', updated_at: null, reason: null, can_start: true, can_stop: false },
    busy: false, error: null, ...overrides };
}
const props = { loading: false, pending: null, error: null, onRefresh: () => {}, onExecute: async () => true };
function hidden(value: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value });
  fireEvent(document, new Event('visibilitychange'));
}

describe('operations room controls', () => {
  it('shows service state and sends only the explicit mirror start or stop action', async () => {
    const onExecute = vi.fn().mockResolvedValue(true);
    const data = operations();
    const view = render(<OperationsStatus {...props} data={data} onExecute={onExecute}/>);
    expect(screen.getByText('정상')).toBeTruthy();
    expect(screen.getByText('정지')).toBeTruthy();
    expect(screen.getByRole('button', { name: '타임폴리오 연동 정지' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: '타임폴리오 연동 시작' }));
    expect(onExecute).toHaveBeenCalledExactlyOnceWith({ action: 'mirror_start' });
    view.rerender(<OperationsStatus {...props} data={operations({ mirror: {
      ...data.mirror, status: 'paused', can_start: false, can_stop: true,
    } })} onExecute={onExecute}/>);
    expect(screen.getByText('신규 주문 정지')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '타임폴리오 연동 정지' }));
    expect(onExecute).toHaveBeenLastCalledWith({ action: 'mirror_stop' });
    expect(onExecute).toHaveBeenCalledTimes(2);
  });

  it('never treats an unverified runner as healthy or allows controls from inconsistent flags', () => {
    const data = operations();
    render(<OperationsStatus {...props} data={operations({ mirror: { ...data.mirror,
      status: 'unverified', reason: '기존 실행기를 확인하지 못했습니다.', can_start: true, can_stop: true,
    } })}/>);
    expect(screen.getByText('실행 확인 필요')).toBeTruthy();
    for (const name of ['타임폴리오 연동 시작', '타임폴리오 연동 정지']) {
      expect(screen.getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
    }
  });

  it('blocks commands while busy or after a failed refresh without presenting stale state as healthy', () => {
    const data = operations();
    const view = render(<OperationsStatus {...props} data={{ ...data, busy: true }}/>);
    expect(screen.getByText('실행 요청 처리 중')).toBeTruthy();
    expect(screen.getByRole('button', { name: '타임폴리오 연동 시작' }).hasAttribute('disabled')).toBe(true);
    view.rerender(<OperationsStatus {...props} data={data} error="실행 상태 조회 실패"/>);
    expect(screen.queryByText('정상')).toBeNull();
    expect(screen.getAllByText('확인 필요')).toHaveLength(2);
    expect(screen.getByRole('alert').textContent).toBe('실행 상태 조회 실패');
    expect(screen.getByRole('button', { name: '타임폴리오 연동 시작' }).hasAttribute('disabled')).toBe(true);
  });

  it('reports an old server clearly even when the 404 body is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 404, ok: false,
      json: () => Promise.reject(new Error('HTML page')) }));
    render(<OperationsPanel/>);
    expect((await screen.findByRole('alert')).textContent).toContain('서버 업데이트가 필요합니다');
    expect(screen.getByRole('button', { name: '타임폴리오 연동 시작' }).hasAttribute('disabled')).toBe(true);
  });

  it('validates required state and capabilities before exposing controls', () => {
    const data = operations();
    for (const value of [null, {}, { ...data, busy: 'false' },
      { ...data, mirror: { ...data.mirror, status: 'healthy' } },
      { ...data, mirror: { ...data.mirror, can_start: 'true' } },
      { ...data, mirror: { ...data.mirror, can_stop: undefined } }]) {
      expect(() => readOperations(value)).toThrow();
    }
  });
});

describe('operations API requests', () => {
  it('loads with the dashboard header and accepts asynchronous command results without claiming ready', async () => {
    const data = operations();
    const starting = operations({ busy: true, mirror: { ...data.mirror, status: 'initialized', can_start: false } });
    const fetchMock = vi.fn().mockResolvedValueOnce(response(data)).mockResolvedValueOnce(response(starting, 202));
    vi.stubGlobal('fetch', fetchMock);
    render(<OperationsPanel/>);
    await waitFor(() => expect(screen.getByRole('button', { name: '타임폴리오 연동 시작' }).hasAttribute('disabled')).toBe(false));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/operations');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'GET', headers: { 'X-KIS-Dashboard': '1' } });
    await userEvent.click(screen.getByRole('button', { name: '타임폴리오 연동 시작' }));
    expect(await screen.findByText('연결 확인 중')).toBeTruthy();
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'POST', body: '{"action":"mirror_start"}',
      headers: { 'X-KIS-Dashboard': '1', 'Content-Type': 'application/json' } });
    expect(screen.getByRole('button', { name: '타임폴리오 연동 시작' }).hasAttribute('disabled')).toBe(true);
  });

  it('does not duplicate a command on repeated requests or visibility changes', async () => {
    const command = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(operations())).mockReturnValueOnce(command.promise);
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useOperations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    let action: Promise<boolean>;
    act(() => { action = result.current.execute({ action: 'mirror_start' }); });
    await act(async () => { expect(await result.current.execute({ action: 'mirror_start' })).toBe(false); });
    act(() => { hidden(true); hidden(false); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { command.resolve(response(operations())); await action; });
    expect(fetchMock.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  });

  it('ignores an older poll response after an explicit command', async () => {
    const old = deferred<Response>();
    const data = operations();
    const paused = operations({ mirror: { ...data.mirror, status: 'paused', can_start: false, can_stop: true } });
    const fetchMock = vi.fn().mockResolvedValueOnce(response(data)).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(response(paused));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useOperations());
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => { void result.current.refresh(); });
    await act(async () => { await result.current.execute({ action: 'mirror_start' }); });
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
    await act(async () => { old.resolve(response(data)); });
    expect(result.current.data?.mirror.status).toBe('paused');
  });

  it('times out an ambiguous command without automatically submitting it again', async () => {
    vi.useFakeTimers();
    const command = deferred<Response>();
    const fetchMock = vi.fn().mockResolvedValueOnce(response(operations())).mockReturnValueOnce(command.promise)
      .mockReturnValue(deferred<Response>().promise);
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useOperations());
    await act(async () => {});
    act(() => { void result.current.execute({ action: 'mirror_stop' }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
    expect(result.current.error).toContain('실행 요청 결과를 확인하지 못했습니다');
    expect(result.current.pending).toBeNull();
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
    expect(fetchMock.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
    await act(async () => { command.resolve(response(operations())); });
    expect(result.current.error).toContain('실행 요청 결과를 확인하지 못했습니다');
  });

  it('aborts pending status reads when leaving the operations room', async () => {
    const fetchMock = vi.fn().mockReturnValue(deferred<Response>().promise);
    vi.stubGlobal('fetch', fetchMock);
    const view = renderHook(() => useOperations());
    view.unmount();
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
  });
});
