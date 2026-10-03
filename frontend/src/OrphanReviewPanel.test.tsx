import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { OrphanReviewPanel } from './OrphanReviewPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('orphan acknowledgment requires explicit review and retains recovery blockers', async () => {
  const item = { position_id: 'orphan-1', trading_symbol: 'FIXTURE', quantity: 10, observed_average_price: '100',
    observed_at: '2026-10-01T00:00:00Z', head_hash: 'a'.repeat(64), acknowledged: false,
    blockers: ['HISTORY_NOT_RECONSTRUCTED', 'ENTRIES_BLOCKED'] };
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => new Response(JSON.stringify(
    options?.method === 'POST' ? { ...item, acknowledged: true } : { items: [item], has_more: false }
  )));
  vi.stubGlobal('fetch', fetcher);
  render(<OrphanReviewPanel api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review orphan orphan-1' }));
  const submit = screen.getByRole('button', { name: 'Record orphan acknowledgment' });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Orphan acknowledgment reason'), { target: { value: 'Owner reviewed orphan evidence' } });
  fireEvent.change(screen.getByLabelText('Type ACKNOWLEDGE PAPER ORPHAN'), { target: { value: 'ACKNOWLEDGE PAPER ORPHAN' } });
  fireEvent.click(submit);
  expect(await screen.findByRole('status')).toHaveTextContent('Entries remain blocked');
  const post = fetcher.mock.calls.find(([, options]) => options?.method === 'POST');
  expect(JSON.parse(String(post?.[1]?.body)).expected_head).toBe(item.head_hash);
  expect(screen.getByText(/HISTORY_NOT_RECONSTRUCTED/)).toBeInTheDocument();
});

test('unavailable orphan evidence is not shown as successful empty recovery', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Evidence invalid' }), { status: 409 })));
  render(<OrphanReviewPanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('ORPHAN REVIEW UNAVAILABLE');
  expect(screen.queryByText(/No recorded orphans/)).not.toBeInTheDocument();
});

test('recovery inspection is read-only and clears the old plan on refresh', async () => {
  const item = { position_id: 'orphan-1', acknowledged: true, acknowledged_by: 'owner', blockers: ['ENTRIES_BLOCKED'] };
  const fetcher = vi.fn(async (url: string, _options?: RequestInit) => new Response(JSON.stringify(
    url.includes('/recovery-plan') ? { original_position_id: 'original-1', quantity: 150, gross_realised_pnl: '400' }
      : { items: [item], has_more: false }
  )));
  vi.stubGlobal('fetch', fetcher);
  render(<OrphanReviewPanel api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Inspect recovery plan orphan-1' }));
  expect(await screen.findByRole('region', { name: 'Verified historical recovery plan' })).toHaveTextContent('does not restore the position or arm trading');
  expect(fetcher.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh orphan evidence' }));
  expect(screen.queryByRole('region', { name: 'Verified historical recovery plan' })).not.toBeInTheDocument();
});
