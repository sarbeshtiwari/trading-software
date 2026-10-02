import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { PaperSummaryPanel } from './PaperSummaryPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('session reports distinguish loading, empty and unavailable without stale figures', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ reports: [], generated_at: '2026-09-18T10:00:00Z' })));
  vi.stubGlobal('fetch', fetcher);
  render(<PaperSummaryPanel api={new Api()} />);
  expect(screen.getByText('Loading session summaries…')).toBeInTheDocument();
  expect(await screen.findByText('No recorded PAPER session summaries.')).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response('{"detail":"PAPER_SUMMARY_INTEGRITY_FAILURE"}', { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh session summaries' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('PAPER_SUMMARY_INTEGRITY_FAILURE');
  expect(screen.queryByText('No recorded PAPER session summaries.')).not.toBeInTheDocument();
});

test.each([false, true])('session report distinguishes observed and unavailable history (%s)', async (historical) => {
  const fixture = { generated_at: '2026-09-18T10:00:00Z', reports: [{
    audit_id: 'isolated-audit', chain_id: 'isolated-chain', sequence: 2,
    event_type: 'PAPER_DAILY_SUMMARY_RECOVERY', recorded_at: '2026-09-18T10:00:00Z',
    summary: {
      session_date: '2026-09-18', session_close: '2026-09-18T10:00:00Z', as_of: '2026-09-18T10:00:00Z',
      mode: 'PAPER', execution_realism: 'SIMULATED', scope: 'POST_CLOSE_RECOVERY_OBSERVATION', cycle_error: null,
      worker_review_required: true, reconciliation: 'COMPLETED_THIS_CYCLE', fill_count: 2, traded_position_count: 1,
      fill_ids: ['isolated-entry', 'isolated-exit'], gross_realised_pnl: '123.45', charges: null, net_realised_pnl: null,
      cost_status: 'UNAVAILABLE', open_positions: [], unresolved_order_ids: ['pending-fixture'], critical_event_ids: ['isolated-error'],
      order_hygiene: { scope: 'Recorded session actions only', protection_failure_ids: ['protection-fixture'], cancellations: [{ audit_id: 'cancel-fixture', chain_id: 'cancel-chain', order_id: 'entry-fixture', reason: 'SESSION_CUTOFF', status: 'CANCELLED', filled_quantity: 100, terminal: true, error: null }] },
      limit_utilisation: { positions_used: 0, max_concurrent_positions: 5, risk_config_version: 1,
        daily_loss: null, drawdown: null, exposure: null, unavailable_reason: 'ACCOUNT_CHARGES_UNAVAILABLE' },
      valuation: { status: 'UNAVAILABLE', reason: 'ACCOUNT_CHARGES_UNAVAILABLE' }, previous_summary_id: 'isolated-first',
    },
  }] };
  const reply = historical ? { ...fixture, reports: fixture.reports.map(report => ({ ...report,
    summary: { ...report.summary, scope: 'MISSED_SESSION_RECOVERY', worker_review_required: null,
      position_history_available: false, order_history_available: false, error_history_available: true,
      reconstruction_status: 'CUTOFF_FILL_EVIDENCE_UNAVAILABLE', gross_realised_pnl: null, fill_count: null },
  })) } : fixture;
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(reply))));
  render(<PaperSummaryPanel api={new Api()} />);
  if (historical) {
    expect(await screen.findByText(/Historical evidence: CUTOFF_FILL_EVIDENCE_UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText(/owner review: UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText('Open positions').nextElementSibling).toHaveTextContent('UNAVAILABLE');
    expect(screen.getByText('Unresolved orders').nextElementSibling).toHaveTextContent('UNAVAILABLE');
    expect(screen.queryByText('123.45')).not.toBeInTheDocument();
  } else {
    expect(await screen.findByText('123.45')).toBeInTheDocument();
    expect(screen.getByText(/owner review: REQUIRED/)).toBeInTheDocument();
  }
  expect(screen.getByText(/Previous observation: isolated-first/)).toBeInTheDocument();
  expect(screen.getByText('daily loss: UNAVAILABLE')).toBeInTheDocument();
  expect(screen.queryByText('Trading Active')).not.toBeInTheDocument();
  expect(screen.getByText(/entry-fixture: SESSION_CUTOFF \/ CANCELLED/)).toBeInTheDocument();
  expect(screen.getByText(/Recorded protection failure IDs: protection-fixture/)).toBeInTheDocument();
});
