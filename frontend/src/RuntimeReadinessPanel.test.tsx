import { render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { RuntimeReadinessPanel } from './RuntimeReadinessPanel';

afterEach(() => vi.unstubAllGlobals());

test('runtime blockers and unavailable evidence are not presented as healthy', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    generated_at: '2026-10-02T11:00:00Z', trading_mode: 'PAPER', execution_worker_enabled: false,
    worker_state: 'DISABLED', entry_gate_open: false, blockers: ['DAILY_LOSS_LIMIT'],
    health_state: 'STALE', health_checked_at: null, calendar_warning: 'Coverage incomplete',
    instrument_count: 0, restricted_instrument_count: 0, instrument_snapshot_id: null,
    instrument_snapshot_received_at: null, instrument_snapshot_source: null,
    instrument_snapshot_audit_verified: null, groww_live_execution: 'UNVERIFIED',
  }))));
  render(<RuntimeReadinessPanel api={new Api()} />);
  expect(await screen.findByText('TRADING DISARMED — ENTRY GATE BLOCKED')).toBeInTheDocument();
  expect(screen.getByText('DAILY_LOSS_LIMIT')).toBeInTheDocument();
  expect(screen.getByText('Snapshot audit: UNAVAILABLE')).toBeInTheDocument();
  expect(screen.getByText(/Health: STALE/)).toBeInTheDocument();
  expect(screen.queryByText('System Healthy')).not.toBeInTheDocument();
});

test('failed readiness request shows an error rather than empty success', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<RuntimeReadinessPanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('READINESS UNAVAILABLE');
  expect(screen.queryByText(/ENTRY GATE OPEN/)).not.toBeInTheDocument();
});
