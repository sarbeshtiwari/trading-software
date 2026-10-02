import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { HealthHistoryPanel } from './HealthHistoryPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('historical pagination freezes the server interval and empty history is not healthy', async () => {
  const fetcher = vi.fn(async (_url: string) => new Response(JSON.stringify({ mode: 'PAPER', from_at: '2026-10-01T00:00:00Z', to_at: '2026-10-02T00:00:00Z', preceding: null, observations: [], has_more: true, next_offset: 50, scope: 'Observations only' })));
  vi.stubGlobal('fetch', fetcher);
  render(<HealthHistoryPanel api={new Api()} />);
  expect(await screen.findByText(/No transitions recorded/)).toHaveTextContent('not proof of healthy service');
  fireEvent.click(screen.getByRole('button', { name: 'Next health observations' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  const url = new URL(String(fetcher.mock.calls[1][0]), 'http://fixture');
  expect(url.searchParams.get('from_at')).toBe('2026-10-01T00:00:00Z');
  expect(url.searchParams.get('to_at')).toBe('2026-10-02T00:00:00Z');
  expect(url.searchParams.get('offset')).toBe('50');
});

test('failed history retrieval shows unavailable, not an empty successful result', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Evidence unavailable' }), { status: 409 })));
  render(<HealthHistoryPanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('HEALTH HISTORY UNAVAILABLE');
  expect(screen.queryByText(/No transitions recorded/)).not.toBeInTheDocument();
});
