import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { MarketPanel } from './MarketPanel';

vi.mock('./CandleChart', () => ({ CandleChart: () => <div>Stored chart rendered</div> }));
afterEach(() => vi.unstubAllGlobals());

test('catalog and interval changes use real API selections and clear unavailable charts', async () => {
  const requests: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    requests.push(url);
    return new Response(JSON.stringify(url.includes('/instruments') ? {
      instruments: [{ id: 'fixture', symbol: 'TEST', exchange: 'NSE', segment: 'CASH', active: true, restricted: true }], has_more: false,
    } : {
      instrument_id: 'fixture', origin: 'LIVE', interval_minutes: url.includes('interval=5') ? 5 : 1,
      status: url.includes('interval=5') ? 'UNAVAILABLE' : 'STALE', as_of: '2026-10-02T10:00:00Z', discontinuities: 0,
      bars: url.includes('interval=5') ? [] : [{ ts: '2026-10-02T09:00:00Z', close: '125.50', volume: 1000, sma20: null, ingested_at: '2026-10-02T09:01:00Z' }],
    }));
  }));
  render(<MarketPanel api={new Api()} />);
  await screen.findByRole('option', { name: 'NSE/CASH/TEST — RESTRICTED' });
  fireEvent.change(screen.getByLabelText('Chart instrument'), { target: { value: 'fixture' } });
  expect(await screen.findByText('Stored chart rendered')).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('MARKET DATA STALE');
  expect(screen.getByText('125.50')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Chart interval'), { target: { value: '5' } });
  expect(await screen.findByText('LIVE: UNAVAILABLE')).toBeInTheDocument();
  expect(screen.queryByText('Stored chart rendered')).not.toBeInTheDocument();
  expect(requests.some(url => url.endsWith('/market/candles/fixture?origin=LIVE&interval=5'))).toBe(true);
});

test('failed catalog does not present fabricated instruments', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<MarketPanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('CATALOG UNAVAILABLE');
  expect(screen.queryByLabelText('Chart instrument')).not.toBeInTheDocument();
});
