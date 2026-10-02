import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api, type Workspace } from './api';
import { OrderControls } from './OrderControls';

afterEach(() => vi.unstubAllGlobals());
const order: Workspace['orders'][number] = {
  id: 'entry-one', role: 'ENTRY', mode: 'PAPER', segment: 'CASH', trading_symbol: 'TEST',
  status: 'PARTIALLY_FILLED', quantity: 200, filled_quantity: 100, average_fill_price: '100',
  rejection_reason: null, created_at: '2026-10-02T05:00:00Z', proposal_id: 'proposal', execution_realism: 'SIMULATED',
};

test('cancel requires confirmation and preserves request identity after unknown outcome', async () => {
  const fetcher = vi.fn()
    .mockRejectedValueOnce(new Error('isolated network failure'))
    .mockResolvedValueOnce(new Response(JSON.stringify({
      order_id: order.id, audit_chain_id: 'chain', status: 'CANCELLED', filled_quantity: 100,
      reason: 'OWNER_CANCEL', terminal: true, error: null,
    })));
  vi.stubGlobal('fetch', fetcher);
  render(<OrderControls api={new Api()} orders={[order]} mode="PAPER" onChange={vi.fn()} />);
  const button = screen.getByRole('button', { name: 'Cancel entry remainder' });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Entry order'), { target: { value: order.id } });
  fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'Cancel isolated test entry' } });
  fireEvent.change(screen.getByLabelText('Type CANCEL PAPER ENTRY'), { target: { value: 'CANCEL PAPER ENTRY' } });
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('Outcome is not confirmed');
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(await screen.findByRole('status')).toHaveTextContent('CANCELLED; filled quantity: 100');
  expect(fetcher.mock.calls[0][1].body).toBe(fetcher.mock.calls[1][1].body);
});

test('protective exits and executed entries are not selectable', () => {
  render(<OrderControls api={new Api()} orders={[
    { ...order, role: 'EXIT' }, { ...order, id: 'filled', status: 'EXECUTED' },
  ]} mode="PAPER" onChange={vi.fn()} />);
  expect(screen.getByText('No cancellable PAPER entry orders recorded.')).toBeInTheDocument();
  expect(screen.getByRole('button')).toBeDisabled();
});

test('non-PAPER mode exposes no cancellation action', () => {
  render(<OrderControls api={new Api()} orders={[order]} mode="SUPERVISED" onChange={vi.fn()} />);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

test('bulk entries require their own confirmation and report actual unresolved outcomes', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
    audit_chain_id: 'bulk-chain', resolved: false, excluded_exit_ids: ['protected-exit'],
    outcomes: [{ order_id: 'entry-one', status: 'UNKNOWN', filled_quantity: 100, error: 'LOOKUP_FAILED' }],
  })));
  vi.stubGlobal('fetch', fetcher);
  render(<OrderControls api={new Api()} orders={[order]} mode="PAPER" onChange={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Entry order'), { target: { value: '__ALL__' } });
  fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'Cancel pending PAPER entries' } });
  fireEvent.change(screen.getByLabelText('Type CANCEL PAPER ENTRIES'), { target: { value: 'CANCEL PAPER ENTRY' } });
  expect(screen.getByRole('button')).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Type CANCEL PAPER ENTRIES'), { target: { value: 'CANCEL PAPER ENTRIES' } });
  fireEvent.click(screen.getByRole('button'));
  expect(await screen.findByRole('status')).toHaveTextContent('UNRESOLVED');
  expect(screen.getByRole('status')).toHaveTextContent('protected-exit');
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/orders/cancel-entries');
});
