import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { AuditPanel } from './AuditPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('audit exposes verified evidence gaps and clears failed drill-down', async () => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(url.includes('/trails/') ? {
    requested_id: 'fixture', scope: 'Not proof of completeness', gaps: ['fixture: CORRUPT'],
    chains: [{ id: 'fixture', integrity: 'CORRUPT', records: [] }], journals: [],
  } : { events: [], has_more: false })));
  vi.stubGlobal('fetch', fetcher);
  render(<AuditPanel api={new Api()} />);
  expect(await screen.findByText('No recorded events match.')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Decision, proposal, trade or audit ID'), { target: { value: 'fixture' } });
  fireEvent.click(screen.getByRole('button', { name: 'Inspect ID' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('CORRUPT');
  fetcher.mockImplementation(async () => new Response('{"detail":"Unavailable"}', { status: 503 }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect ID' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('AUDIT UNAVAILABLE');
  expect(screen.queryByText('Trail fixture')).not.toBeInTheDocument();
});
