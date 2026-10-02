import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { JournalPanel } from './JournalPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('journal filters real requests and distinguishes empty records from unavailable service', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ entries: [], has_more: false, scope: 'Recorded fixture only' }), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  render(<JournalPanel api={new Api()} />);
  expect(await screen.findByText('No journal entries match these filters.')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Journal kind'), { target: { value: 'REJECTION' } });
  fireEvent.change(screen.getByLabelText('strategy id (journal)'), { target: { value: 'isolated strategy' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply journal filters' }));
  await waitFor(() => expect(fetcher).toHaveBeenLastCalledWith(expect.stringContaining('strategy_id=isolated+strategy&kind=REJECTION'), expect.anything()));
  fetcher.mockImplementation(async () => new Response('{"detail":"Integrity check failed"}', { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh journal' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Journal unavailable or stale');
  expect(screen.queryByText('No journal entries match these filters.')).not.toBeInTheDocument();
});

test('export refreshes authentication and refuses unavailable downloads', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response('{}', { status: 401 }))
    .mockResolvedValueOnce(new Response('{"access_token":"isolated-download-token"}', { status: 200 }))
    .mockResolvedValueOnce(new Response('"id"\r\n"fixture"', { status: 200 }))
    .mockResolvedValueOnce(new Response('{"detail":"Narrow journal filters"}', { status: 413 }));
  vi.stubGlobal('fetch', fetcher);
  const api = new Api();
  const blob = await api.download('/journal/export/csv');
  expect(await blob.text()).toContain('fixture');
  expect(fetcher.mock.calls[2][1].headers.Authorization).toBe('Bearer isolated-download-token');
  await expect(api.download('/journal/export/json')).rejects.toMatchObject({ status: 413, message: 'Narrow journal filters' });
});
