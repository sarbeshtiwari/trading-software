import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { WorkspaceUpdates } from './WorkspaceUpdates';
import { App } from './App';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('order messages request authoritative reads rather than apply event payloads', async () => {
  vi.stubGlobal('WebSocket', class {});
  const socket = { onmessage: undefined as undefined | ((event: { data: string }) => void), onclose: undefined as undefined | ((event: { code: number }) => void), close: vi.fn() };
  const api = new Api();
  vi.spyOn(api, 'workspaceStream').mockReturnValue(socket as unknown as WebSocket);
  const refresh = vi.fn(async () => {});
  render(<WorkspaceUpdates api={api} refresh={refresh} />);
  socket.onmessage!({ data: JSON.stringify({ type: 'WORKSPACE_REFRESH', account: { equity: 'fabricated' } }) });
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  expect(screen.queryByText('fabricated')).not.toBeInTheDocument();
  socket.onmessage!({ data: JSON.stringify({ type: 'WORKSPACE_HEARTBEAT' }) });
  expect(refresh).toHaveBeenCalledTimes(1);
  socket.onmessage!({ data: 'invalid' });
  expect(socket.close).toHaveBeenCalled();
  socket.onclose!({ code: 1011 });
  expect(await screen.findByText(/Order stream disconnected/)).toBeInTheDocument();
  expect(refresh).toHaveBeenCalledTimes(2);
});

test('concurrent refreshes are coalesced and cannot overwrite newer account state', async () => {
  const pending: Array<(response: Response) => void> = [];
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.endsWith('/auth/refresh')) return Promise.resolve(new Response(JSON.stringify({ access_token: 'test-token' })));
    return new Promise<Response>(resolve => pending.push(resolve));
  }));
  const api = new Api();
  vi.spyOn(api, 'workspaceStream').mockReturnValue({ close: vi.fn() } as unknown as WebSocket);
  render(<App api={api} />);
  const button = await screen.findByRole('button', { name: 'Refresh' });
  fireEvent.click(button); fireEvent.click(button);
  expect(pending).toHaveLength(1);
  const workspace = { trading_mode: 'PAPER', generated_at: '2026-10-03T00:00:00Z',
    new_entries_allowed: false, blockers: [], account_status: 'UNAVAILABLE', regime_status: 'UNAVAILABLE',
    positions: [], orders: [], fills: [], decisions: [], audit: [], strategies: [], chains: [], preflights: [], journal: [], components: [] };
  pending[0](new Response(JSON.stringify(workspace)));
  await waitFor(() => expect(pending).toHaveLength(2));
  pending[1](new Response(JSON.stringify({ ...workspace, blockers: ['LATEST_RISK_BLOCKER'] })));
  expect(await screen.findByText(/LATEST_RISK_BLOCKER/)).toBeInTheDocument();
  expect(pending).toHaveLength(2);
});
