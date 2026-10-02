import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { ReconciliationPanel } from './ReconciliationPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('resolution submits the reviewed evidence hash and explicit owner confirmation', async () => {
  const item = { record: { id: 'discrepancy-1', resolved: false, delta: { quantity: 1 } }, head_hash: 'a'.repeat(64) };
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return new Response(JSON.stringify({ ...item, record: { ...item.record, resolved: true } }));
    return new Response(JSON.stringify({ items: [item], has_more: false, scope: 'PAPER only' }));
  });
  vi.stubGlobal('fetch', fetcher);
  render(<ReconciliationPanel api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review discrepancy-1' }));
  const submit = screen.getByRole('button', { name: 'Record authenticated discrepancy resolution' });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Discrepancy review reason'), { target: { value: 'Reviewed restored accounting' } });
  fireEvent.change(screen.getByLabelText('Type RESOLVE PAPER DISCREPANCY'), { target: { value: 'RESOLVE PAPER DISCREPANCY' } });
  fireEvent.click(submit);
  expect(await screen.findByRole('status')).toHaveTextContent('Other risk, health and emergency gates remain');
  const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
  expect(JSON.parse(String(post?.[1]?.body))).toEqual({ expected_head: item.head_hash, reason: 'Reviewed restored accounting', confirmation: 'RESOLVE PAPER DISCREPANCY' });
});

test('failed evidence reads are unavailable, not successful empty reconciliation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Evidence invalid' }), { status: 409 })));
  render(<ReconciliationPanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('RECONCILIATION UNAVAILABLE');
  expect(screen.queryByText(/No recorded discrepancies/)).not.toBeInTheDocument();
});

test('superseded reads cannot replace the current discrepancy view', async () => {
  let finishOld: (response: Response) => void = () => {};
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { finishOld = resolve; }))
    .mockResolvedValue(new Response(JSON.stringify({ items: [], has_more: false, scope: 'Latest evidence' })));
  vi.stubGlobal('fetch', fetcher);
  render(<ReconciliationPanel api={new Api()} />);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh discrepancies' }));
  expect(await screen.findByText('Latest evidence')).toBeInTheDocument();
  finishOld(new Response(JSON.stringify({ items: [], has_more: false, scope: 'Obsolete evidence' })));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(screen.queryByText('Obsolete evidence')).not.toBeInTheDocument();
});
