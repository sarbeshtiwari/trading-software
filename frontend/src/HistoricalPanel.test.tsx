import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { HistoricalPanel } from './HistoricalPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('historical inspection displays real response status, undefined ratios and curve gaps', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/historical-jobs')) return new Response('[]', { status: 200 });
    let body: object = { runs: [{ id: 'fixture01', status: 'INCOMPLETE', progress_pct: '100' }], has_more: false, scope: 'Configured database only' };
    if (url.includes('/samples')) body = { samples: [
      { audit_event_id: 'fixture-audit-one', observed_at: '2026-09-21T04:30:00Z', baseline: true, status: 'ESTIMATED', net_equity: '100000', drawdown: '0' },
      { audit_event_id: 'fixture-audit-two', observed_at: '2026-09-21T04:30:05Z', baseline: false, status: 'UNAVAILABLE', net_equity: null, drawdown: null },
    ], has_more: false };
    else if (url.includes('/trades')) body = { trades: [], has_more: false };
    else if (url.endsWith('/fixture01')) body = { run: { status: 'INCOMPLETE', progress_pct: '100', data_window_warning: 'Incomplete fixture coverage' }, results: [{ id: 'fixture-result', net_pnl: null, sharpe: null, notes: 'SIMULATED fixture' }], recording_sha256: 'fixture-hash', fill_convention: 'RECORDED_DEPTH_AT_PUBLICATION', launch_available: false };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  render(<HistoricalPanel api={new Api()} />);
  await screen.findByRole('option', { name: /fixture01/ });
  fireEvent.change(screen.getByLabelText('Historical run'), { target: { value: 'fixture01' } });
  expect(await screen.findByRole('heading', { name: 'INCOMPLETE — 100%' })).toBeInTheDocument();
  expect(screen.getByText('Unavailable samples: 1')).toBeInTheDocument();
  expect(screen.getAllByText('UNAVAILABLE')).toHaveLength(2);
  expect(screen.getByRole('img', { name: 'Sampled net equity' })).toBeInTheDocument();
  expect(screen.getByRole('img', { name: 'Sampled drawdown fraction' })).toBeInTheDocument();
  expect(screen.getByText('No recorded closed trades.')).toBeInTheDocument();
  expect(screen.getByText(/this report has no structured universe disclosure/)).toBeInTheDocument();
  expect(screen.getByText(/this sealed report has no captured window-duration policy/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /launch/i })).not.toBeInTheDocument();
});

test('historical view distinguishes empty data from unavailable service', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.includes('/historical-jobs') ? [] : { runs: [], has_more: false, scope: 'Configured database only' }), { status: 200 })));
  const view = render(<HistoricalPanel api={new Api()} />);
  expect(await screen.findByText('No recorded historical runs in this database.')).toBeInTheDocument();
  view.unmount();
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<HistoricalPanel api={new Api()} />);
  expect(await screen.findByText(/HISTORICAL DATA UNAVAILABLE/)).toBeInTheDocument();
  expect(screen.queryByText('No recorded historical runs in this database.')).not.toBeInTheDocument();
});
