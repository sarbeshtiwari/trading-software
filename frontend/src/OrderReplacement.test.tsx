import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api, type Workspace } from './api';
import { OrderReplacement } from './OrderReplacement';

afterEach(() => vi.unstubAllGlobals());
const order: Workspace['orders'][number] = {
  id: 'entry', role: 'ENTRY', parent_order_id: null, mode: 'PAPER', segment: 'CASH',
  trading_symbol: 'FIXTURE', status: 'OPEN', quantity: 250, filled_quantity: 0,
  average_fill_price: null, rejection_reason: null, created_at: '2026-10-02T05:00:00Z',
  proposal_id: 'original', execution_realism: 'SIMULATED',
};

test('replacement requires approved candidate and retains request after network failure', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify([
    { proposal_id: 'fresh', entry: '100.05', stop: '98', target: '104.15', approved_quantity: 243 },
  ]))).mockRejectedValueOnce(new Error('network interrupted')).mockResolvedValueOnce(new Response(JSON.stringify({
    original_status: 'CANCELLED', replacement_status: 'NOT_SUBMITTED',
    code: 'INTERRUPTED_REVIEW_REQUIRED', error: null, audit_chain_id: 'audit',
  })));
  vi.stubGlobal('fetch', fetcher);
  render(<OrderReplacement api={new Api()} orders={[order]} mode="PAPER" onChange={vi.fn()} />);
  const button = screen.getByRole('button');
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByRole('combobox', { name: 'Original entry' }), { target: { value: 'entry' } });
  await screen.findByRole('option', { name: /fresh: entry/ });
  fireEvent.change(screen.getByRole('combobox', { name: 'Replacement approval' }), { target: { value: 'fresh' } });
  fireEvent.change(screen.getByLabelText('Replacement reason'), { target: { value: 'Replace fixture approval' } });
  fireEvent.change(screen.getByLabelText('Type REPLACE PAPER ENTRY'), { target: { value: 'REPLACE PAPER ENTRY' } });
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('Outcome unconfirmed');
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(await screen.findByRole('status')).toHaveTextContent('NOT_SUBMITTED');
  const first = JSON.parse(fetcher.mock.calls[1][1].body);
  const retry = JSON.parse(fetcher.mock.calls[2][1].body);
  expect(retry).toEqual(first);
  expect(first.replacement_proposal_id).toBe('fresh');
  expect(first).not.toHaveProperty('quantity');
});
