import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  expect(screen.getAllByRole('cell', { name: 'UNAVAILABLE', exact: true })).toHaveLength(4);
});
