import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { FnoBanPanel } from './FnoBanPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('report admission uses reviewed head and invalidates conflicting review', async () => {
  const state = { origin: 'SYNTHETIC', status: 'UNAVAILABLE', head_id: 'reviewed', as_of: '2026-10-02T00:00:00Z' };
  const fetcher = vi.fn(async (_url: unknown, init?: RequestInit) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'Ban report changed' }), { status: 409 })
    : new Response(JSON.stringify(state)));
  vi.stubGlobal('fetch', fetcher);
  render(<FnoBanPanel api={new Api()} onChange={() => undefined} />);
  expect(screen.getByRole('button', { name: 'Review ban report state' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Ban report origin'), { target: { value: 'SYNTHETIC' } });
  fireEvent.click(screen.getByRole('button', { name: 'Review ban report state' }));
  const editor = await screen.findByLabelText('NSE ban CSV');
  expect(editor).toHaveValue('');
  fireEvent.change(editor, { target: { value: 'Securities in Ban For Trade Date 02-OCT-2026:\n1,TEST' } });
  fireEvent.change(screen.getByLabelText('Report known at (ISO timestamp with timezone)'), { target: { value: '2026-10-02T00:00:00Z' } });
  fireEvent.change(screen.getByLabelText('Ban report review reason'), { target: { value: 'Owner reviews report fixture' } });
  fireEvent.change(screen.getByLabelText('Type ADMIT PAPER NSE BAN REPORT'), { target: { value: 'ADMIT PAPER NSE BAN REPORT' } });
  fireEvent.click(screen.getByRole('button', { name: 'Admit reviewed ban report' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Ban report changed');
  expect(screen.queryByLabelText('NSE ban CSV')).not.toBeInTheDocument();
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ expected_event_id: 'reviewed', origin: 'SYNTHETIC' });
});
