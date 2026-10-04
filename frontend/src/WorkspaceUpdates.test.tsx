import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { WorkspaceUpdates } from './WorkspaceUpdates';
import { App } from './App';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

test('order messages request authoritative reads rather than apply event payloads', async () => {
  vi.stubGlobal('WebSocket', class {});
  const socket = { onmessage: undefined as undefined | ((event: { data: string }) => void), onclose: undefined as undefined | ((event: { code: number }) => void), close: vi.fn() };
  const api = new Api();
  vi.spyOn(api, 'workspaceStream').mockReturnValue(socket as unknown as WebSocket);
  const refresh = vi.fn(async () => {});
  render(<WorkspaceUpdates api={api} refresh={refresh} />);
  act(() => socket.onmessage!({ data: JSON.stringify({ type: 'WORKSPACE_REFRESH', account: { equity: 'fabricated' } }) }));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  expect(screen.queryByText('fabricated')).not.toBeInTheDocument();
  act(() => socket.onmessage!({ data: JSON.stringify({ type: 'WORKSPACE_HEARTBEAT' }) }));
  expect(refresh).toHaveBeenCalledTimes(1);
  act(() => socket.onmessage!({ data: 'invalid' }));
  expect(socket.close).toHaveBeenCalled();
  act(() => socket.onclose!({ code: 1011 }));
  expect(await screen.findByText(/Execution stream disconnected/)).toBeInTheDocument();
  expect(refresh).toHaveBeenCalledTimes(2);
});

test('concurrent refreshes are coalesced and cannot overwrite newer account state', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  const pending: Array<(response: Response) => void> = [];
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.endsWith('/auth/refresh')) return Promise.resolve(new Response(JSON.stringify({ access_token: 'test-token' })));
    return new Promise<Response>(resolve => pending.push(resolve));
  }));
  const api = new Api();
  vi.spyOn(api, 'workspaceStream').mockReturnValue({ close: vi.fn() } as unknown as WebSocket);
  await act(async () => { render(<App api={api} />); });
  const button = await screen.findByRole('button', { name: 'Refresh' });
  await waitFor(() => expect(pending).toHaveLength(1));
  await act(async () => {});
  fireEvent.click(button); fireEvent.click(button);
  expect(pending).toHaveLength(1);
  const workspace = { trading_mode: 'PAPER', generated_at: '2026-10-03T00:00:00Z',
    new_entries_allowed: false, blockers: [], account_status: 'UNAVAILABLE', regime_status: 'UNAVAILABLE',
    positions: [], orders: [], fills: [], decisions: [], audit: [], strategies: [], chains: [], preflights: [], journal: [], components: [] };
  await act(async () => pending[0](new Response(JSON.stringify(workspace))));
  await waitFor(() => expect(pending).toHaveLength(2));
  await act(async () => pending[1](new Response(JSON.stringify({ ...workspace, blockers: ['LATEST_RISK_BLOCKER'] }))));
  expect(await screen.findByText(/LATEST_RISK_BLOCKER/)).toBeInTheDocument();
  expect(pending).toHaveLength(2);
  await act(async () => vi.advanceTimersByTime(9999));
  expect(pending).toHaveLength(2);
  await act(async () => vi.advanceTimersByTime(1));
  expect(pending).toHaveLength(3);
  await act(async () => pending[2](new Response(JSON.stringify({ ...workspace, blockers: ['POLLED_RISK_BLOCKER'] }))));
  expect(await screen.findByText(/POLLED_RISK_BLOCKER/)).toBeInTheDocument();
});

test('workspace refresh updates risk latches without replacing an owner configuration draft', async () => {
  let latched = false;
  const workspace = { trading_mode: 'PAPER', generated_at: '2026-10-03T00:00:00Z',
    new_entries_allowed: false, blockers: [], account_status: 'UNAVAILABLE', regime_status: 'UNAVAILABLE',
    positions: [], orders: [], fills: [], decisions: [], audit: [], strategies: [], chains: [], preflights: [], journal: [], components: [] };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.endsWith('/auth/refresh') ? { access_token: 'test-token' }
      : url.endsWith('/workspace') ? workspace
      : url.endsWith('/risk') ? { status: latched ? 'RISK_LATCHED_FIXTURE' : 'RISK_CLEAR_FIXTURE', limits: { version: 1 }, latches: {} }
      : {};
    return new Response(JSON.stringify(body));
  }));
  const api = new Api();
  vi.spyOn(api, 'workspaceStream').mockReturnValue({ close: vi.fn() } as unknown as WebSocket);
  render(<App api={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Risk' }));
  expect(await screen.findByText('RISK_CLEAR_FIXTURE')).toBeInTheDocument();
  const editor = screen.getByLabelText('Risk configuration JSON');
  fireEvent.change(editor, { target: { value: 'unsaved owner draft' } });
  latched = true;
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  expect(await screen.findByText('RISK_LATCHED_FIXTURE')).toBeInTheDocument();
  expect(editor).toHaveValue('unsaved owner draft');
});
