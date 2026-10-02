import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { QuoteWatchlist } from './QuoteWatchlist';

afterEach(() => vi.unstubAllGlobals());

test('watchlist polls actual selections and suppresses values when stale', async () => {
  let calls = 0;
  vi.stubGlobal('fetch', vi.fn(async () => {
    calls += 1;
    return new Response(JSON.stringify({ instrument_id: 'instrument', origin: 'LIVE', status: calls === 1 ? 'RECORDED' : 'STALE', ltp: '123.45', volume: null, change_pct: null, observed_at: '2026-10-03T10:00:00Z' }));
  }));
  render(<QuoteWatchlist api={new Api()} instrument="instrument" origin="LIVE" />);
  fireEvent.click(screen.getByRole('button', { name: 'Watch selected instrument' }));
  expect(await screen.findByRole('cell', { name: '123.45' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Watch selected instrument' })).toBeDisabled();
  await waitFor(() => expect(screen.getByRole('cell', { name: 'STALE' })).toBeInTheDocument(), { timeout: 4500 });
  expect(screen.queryByRole('cell', { name: '123.45' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Remove instrument' }));
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

test('failed quote does not fabricate a price', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<QuoteWatchlist api={new Api()} instrument="instrument" origin="LIVE" />);
  fireEvent.click(screen.getByRole('button', { name: 'Watch selected instrument' }));
  expect(await screen.findByText(/UNAVAILABLE: Error:/)).toBeInTheDocument();
  expect(screen.getAllByRole('cell', { name: 'UNAVAILABLE' })).toHaveLength(4);
});

test('stream disconnect removes prior prices and reconnects with bounded fallback', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ instrument_id: 'instrument', origin: 'LIVE', status: 'UNAVAILABLE' }))));
  const api = new Api();
  const socket = { onmessage: null as null | ((event: { data: string }) => void), onclose: null as null | ((event: { code: number }) => void), close: vi.fn() };
  const open = vi.spyOn(api, 'quoteStream').mockReturnValue(socket as unknown as WebSocket);
  render(<QuoteWatchlist api={api} instrument="instrument" origin="LIVE" />);
  fireEvent.click(screen.getByRole('button', { name: 'Watch selected instrument' }));
  await waitFor(() => expect(open).toHaveBeenCalledTimes(1));
  act(() => socket.onmessage?.({ data: JSON.stringify({ type: 'QUOTE_SNAPSHOT', quotes: [{ instrument_id: 'instrument', origin: 'LIVE', status: 'RECORDED', ltp: '99.75' }] }) }));
  expect(screen.getByRole('cell', { name: '99.75' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: 'WebSocket' })).toBeInTheDocument();
  act(() => socket.onclose?.({ code: 1011 }));
  expect(screen.queryByRole('cell', { name: '99.75' })).not.toBeInTheDocument();
  await waitFor(() => expect(open).toHaveBeenCalledTimes(2), { timeout: 2500 });
  expect(screen.getByText('HTTP polling — stream disconnected')).toBeInTheDocument();
});
