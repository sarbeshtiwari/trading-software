import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { EventControlsPanel } from './EventControlsPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('event publication pins reviewed origin and version and fails closed on conflict', async () => {
  const state = { instrument_id: 'fixture', origin: 'REPLAY', status: 'BLACKOUT', event_id: 'reviewed-head', event_ids: ['source:event'], control: null };
  const fetcher = vi.fn(async (url: unknown, init?: RequestInit) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'Calendar changed' }), { status: 409 })
    : new Response(JSON.stringify(String(url).includes('/fixture?') ? state : [state])));
  vi.stubGlobal('fetch', fetcher);
  render(<EventControlsPanel api={new Api()} onChange={() => undefined} />);
  expect(await screen.findByText(/fixture \/ REPLAY/)).toHaveTextContent('BLACKOUT');
  fireEvent.change(screen.getByLabelText('Event instrument ID'), { target: { value: 'fixture' } });
  expect(screen.getByRole('button', { name: 'Review event control' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Event data origin'), { target: { value: 'REPLAY' } });
  fireEvent.click(screen.getByRole('button', { name: 'Review event control' }));
  const editor = await screen.findByLabelText('Event control JSON');
  expect(editor).toHaveValue('');
  fireEvent.change(editor, { target: { value: JSON.stringify({ enabled: false, origin: 'LIVE', expected_event_id: 'unreviewed', max_age_seconds: 600, reason: 'Owner reviewed calendar evidence' }) } });
  fireEvent.change(screen.getByLabelText('Type DISABLE PAPER EVENT CONTROL'), { target: { value: 'DISABLE PAPER EVENT CONTROL' } });
  fireEvent.click(screen.getByRole('button', { name: 'Publish reviewed event control' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Calendar changed');
  expect(screen.queryByLabelText('Event control JSON')).not.toBeInTheDocument();
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ origin: 'REPLAY', expected_event_id: 'reviewed-head', enabled: false });
});
