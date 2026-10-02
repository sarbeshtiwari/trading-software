import { render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { RiskUtilisationPanel } from './RiskUtilisationPanel';

afterEach(() => vi.unstubAllGlobals());

test('renders server risk utilisation and disarmed status without implying permission', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    mode: 'PAPER', origin: 'SYNTHETIC', status: 'AVAILABLE', observed_at: '2026-10-02T10:00:00Z',
    evidence_id: 'fixture-audit', configuration_version: 1, strategy_id: 'fixture-strategy',
    latch_code: 'DAILY_LOSS_LATCHED', metrics: [{ name: 'daily_loss', used: '2000', limit: '1800', percent: '111.11' }],
  }))));
  render(<RiskUtilisationPanel api={new Api()} origin="SYNTHETIC" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('TRADING DISARMED');
  expect(screen.getByText('daily loss: 2000 / 1800')).toBeInTheDocument();
  expect(screen.getByText('111.11%')).toBeInTheDocument();
  expect(screen.getByRole('progressbar')).toHaveAttribute('value', '111.11');
});

test('stale evidence and failed refresh never show a healthy utilisation bar', async () => {
  const api = new Api();
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    mode: 'PAPER', origin: 'LIVE', status: 'ACCOUNT_EVIDENCE_STALE', metrics: [],
  }))));
  const { rerender } = render(<RiskUtilisationPanel api={api} origin="LIVE" />);
  expect(await screen.findByText('PAPER / LIVE: ACCOUNT_EVIDENCE_STALE')).toBeInTheDocument();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  rerender(<RiskUtilisationPanel api={api} origin="SYNTHETIC" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('RISK UTILISATION UNAVAILABLE');
  expect(screen.queryByText(/PAPER \/ LIVE:/)).not.toBeInTheDocument();
});
