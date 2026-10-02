import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { RiskDecisionPanel } from './RiskDecisionPanel';

afterEach(() => vi.unstubAllGlobals());

test('shows backend per-trade rejection and withholds corrupted evidence', async () => {
  const row = { id: 'fixture-risk', proposal_id: 'fixture-proposal', evaluated_at: '2026-10-02T10:00:00Z', is_preflight: false };
  let status = 'AUDIT_BOUND_REPLAY_VERIFIED';
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.includes('?offset=') ? { items: [row], has_more: false } : {
    ...row, mode: 'PAPER', origin: 'SYNTHETIC', configuration_version: 1, historical_only: true, status,
    decision: { approved: false, binding_rule: 'per_trade', rejection_code: 'PER_TRADE_RISK_EXCEEDED', formula_version: '1.0.0', rules: [{ rule: 'per_trade', passed: false, rejection_code: 'PER_TRADE_RISK_EXCEEDED', inputs: { risk: '502', limit: '500' } }] },
  }))));
  render(<RiskDecisionPanel api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Inspect risk fixture-risk' }));
  expect(await screen.findByText('Per-trade risk / limit: 502 / 500')).toBeInTheDocument();
  expect(screen.getByText('HISTORICAL REJECTION — PER_TRADE_RISK_EXCEEDED')).toBeInTheDocument();
  status = 'CORRUPT';
  fireEvent.click(screen.getByRole('button', { name: 'Inspect risk fixture-risk' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Decision evidence withheld');
  expect(screen.queryByText('Per-trade risk / limit: 502 / 500')).not.toBeInTheDocument();
});
