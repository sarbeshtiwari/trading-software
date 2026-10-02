import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsProductionControls } from './NewsProductionControls';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('recovery uses current server version and never turns a failed write into success', async () => {
  const fetcher = vi.fn(async (_url: unknown, init?: RequestInit) => init?.method === 'POST'
    ? new Response(JSON.stringify({ detail: 'Concurrent update' }), { status: 409 })
    : new Response(JSON.stringify({ article_id: 'fixture', group_event_id: 'group', event_id: 'head-1', state: 'PENDING', attempts: 1, retry_authorizations: 0, authorized_until: null, result: null, billing_verification: 'UNVERIFIED' })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsProductionControls api={new Api()} />);
  fireEvent.change(screen.getByLabelText('Research group event ID'), { target: { value: 'group' } });
  fireEvent.click(screen.getByRole('button', { name: 'Load research job' }));
  expect(await screen.findByText(/Research job state: PENDING/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Research recovery reason'), { target: { value: 'Review saved fixture receipt' } });
  expect(screen.getByRole('button', { name: 'Authorize one research retry' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Recover saved research receipt' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Concurrent update');
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'POST');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ action: 'RECOVER', expected_event_id: 'head-1' });
  expect(screen.queryByText(/Research job state: PENDING/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Recover saved research receipt' })).toBeDisabled();
});
