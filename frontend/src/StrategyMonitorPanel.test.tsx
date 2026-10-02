import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { StrategyMonitorPanel } from './StrategyMonitorPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('degradation latch stays disabled on breached or unavailable evidence', async () => {
  const data = { strategy_id: 'fixture', version: '1', status: 'BREACHED', auto_disabled: true,
    disable_reason: 'PAPER_DEGRADATION_BREACHED', journal_ids: ['journal-1'], net_pnl: '-125',
    maximum_drawdown_amount: '125', expectancy: '-125', win_rate: '0', blockers: [] };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(data))));
  render(<StrategyMonitorPanel api={new Api()} strategies={[{ strategy_id: 'fixture', version: '1', enabled_paper: false, auto_disabled: true }]} />);
  fireEvent.change(screen.getByLabelText('Monitored strategy'), { target: { value: '["fixture","1"]' } });
  expect(await screen.findByText('STRATEGY AUTO-DISABLED — PAPER_DEGRADATION_BREACHED')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Degradation control reason'), { target: { value: 'Owner reviews isolated losses' } });
  fireEvent.change(screen.getByLabelText('Type RESET STRATEGY DEGRADATION'), { target: { value: 'RESET STRATEGY DEGRADATION' } });
  expect(screen.getByRole('button', { name: 'Reset reviewed degradation latch' })).toBeDisabled();
});

test('healthy reviewed reset sends current audit identities and does not claim enablement', async () => {
  const data = { strategy_id: 'fixture', version: '1', status: 'HEALTHY', auto_disabled: true,
    disable_reason: 'PAPER_DEGRADATION_BREACHED', journal_ids: ['journal-1'], net_pnl: '-125',
    maximum_drawdown_amount: '125', expectancy: '-125', win_rate: '0', blockers: [],
    policy_event_id: 'policy-2', last_event_id: 'disable-1' };
  const fetcher = vi.fn(async (_url: unknown, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === 'POST' ? { audit_event_id: 'reset-1' } : data)));
  vi.stubGlobal('fetch', fetcher);
  render(<StrategyMonitorPanel api={new Api()} strategies={[{ strategy_id: 'fixture', version: '1', enabled_paper: false, auto_disabled: true }]} />);
  fireEvent.change(screen.getByLabelText('Monitored strategy'), { target: { value: '["fixture","1"]' } });
  await screen.findByText('Evidence state: HEALTHY; window trades: 1');
  fireEvent.change(screen.getByLabelText('Degradation control reason'), { target: { value: 'Owner reviewed revised threshold' } });
  fireEvent.change(screen.getByLabelText('Type RESET STRATEGY DEGRADATION'), { target: { value: 'RESET STRATEGY DEGRADATION' } });
  fireEvent.click(screen.getByRole('button', { name: 'Reset reviewed degradation latch' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith('/degradation/reset') && JSON.parse(String(init?.body)).last_event_id === 'disable-1')).toBe(true));
  expect(await screen.findByText(/Reset does not enable trading/)).toBeInTheDocument();
});
