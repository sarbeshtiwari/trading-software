import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { FundamentalPanel } from './FundamentalPanel';

afterEach(() => vi.unstubAllGlobals());

test('source-grounded stale evidence never substitutes an excluded metric', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({
    instrument_id: 'instrument', source: 'manual-source', as_of: '2026-10-02T10:00:00Z',
    max_age_days: 365, status: 'AVAILABLE',
    view: { record_id: 'record', evidence: { period_end: '2025-03-31', known_at: '2025-04-01T10:00:00Z' }, metrics: { pe_ratio: null }, exclusions: { pe_ratio: 'STALE' } },
    valuation: { pe_ratio: null }, quality: {}, health: { score: null }, score: { score: null },
  })));
  vi.stubGlobal('fetch', fetcher);
  const api = new Api();
  const { rerender } = render(<FundamentalPanel api={api} instrument="instrument" />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Fundamentals source'), { target: { value: 'manual-source' } });
  fireEvent.click(screen.getByRole('button', { name: 'Inspect fundamentals' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('FUNDAMENTALS STALE');
  expect(screen.getByRole('cell', { name: 'pe_ratio' })).toBeInTheDocument();
  expect(screen.getAllByRole('cell', { name: 'UNAVAILABLE' })).toHaveLength(2);
  rerender(<FundamentalPanel api={api} instrument="another" />);
  expect(screen.queryByText(/Fundamental record:/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('Fundamentals source')).toHaveValue('');
});

test('request failure displays unavailable evidence rather than numbers', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<FundamentalPanel api={new Api()} instrument="instrument" />);
  fireEvent.change(screen.getByLabelText('Fundamentals source'), { target: { value: 'manual-source' } });
  fireEvent.click(screen.getByRole('button', { name: 'Inspect fundamentals' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('FUNDAMENTALS UNAVAILABLE');
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});
