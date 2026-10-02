import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { InstrumentBlocksPanel } from './InstrumentBlocksPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('manual release uses reviewed version and invalidates failed review', async () => {
  const state = { instrument_id: 'fixture', manually_blocked: true, event_id: 'reviewed-head', catalog_restricted: true, catalog_active: true };
  const fetcher = vi.fn(async (url: unknown, init?: RequestInit) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'Concurrent block change' }), { status: 409 })
    : new Response(JSON.stringify(String(url).endsWith('/fixture') ? state : [state])));
  vi.stubGlobal('fetch', fetcher);
  render(<InstrumentBlocksPanel api={new Api()} onChange={() => undefined} />);
  expect(await screen.findByText(/MANUAL ENTRIES BLOCKED/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Instrument control ID'), { target: { value: 'fixture' } });
  fireEvent.click(screen.getByRole('button', { name: 'Load instrument for review' }));
  const reason = await screen.findByLabelText('Instrument control reason');
  fireEvent.change(reason, { target: { value: 'Reviewed instrument restriction evidence' } });
  expect(screen.getByRole('button', { name: 'Release reviewed manual block' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Type UNBLOCK PAPER INSTRUMENT'), { target: { value: 'UNBLOCK PAPER INSTRUMENT' } });
  fireEvent.click(screen.getByRole('button', { name: 'Release reviewed manual block' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Concurrent block change');
  expect(screen.queryByRole('button', { name: 'Release reviewed manual block' })).not.toBeInTheDocument();
  expect(screen.getByText(/MANUAL ENTRIES BLOCKED/)).toBeInTheDocument();
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ blocked: false, expected_event_id: 'reviewed-head' });
});
