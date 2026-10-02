import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { App } from './App';
import { Api } from './api';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('emergency UI requires typed confirmation and reports unavailable flatten honestly', async () => {
  const workspace = { trading_mode: 'PAPER', generated_at: '2026-09-21T04:30:00Z',
    new_entries_allowed: false, blockers: [], broker_verification: 'GROWW_LIVE_UNVERIFIED',
    account: null, account_status: 'UNAVAILABLE', regime_status: 'UNAVAILABLE',
    positions: [{ id: 'isolated-position', net_quantity: 125, trailing_stop_price: '102', watchdog_observation: 'RECENT_SOFTWARE_CHECK', is_protected: false }], orders: [], fills: [], decisions: [], audit: [], strategies: [], chains: [], preflights: [], journal: [], components: [] };
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    let body: object = workspace;
    if (url.endsWith('/auth/refresh')) body = { access_token: 'isolated-ui-token' };
    else if (url.endsWith('/risk')) body = { status: 'UNAVAILABLE', limits: null, latches: {} };
    else if (url.endsWith('/emergency')) body = init?.method === 'POST'
      ? { entries_blocked: true, kill_switch: false, execution: 'WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED', outcomes: [] }
      : { entries_blocked: false, kill_switch: false, execution: 'STATE_ONLY', outcomes: [] };
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'Risk' }));
  fireEvent.change(await screen.findByLabelText('Emergency action'), { target: { value: 'FLATTEN' } });
  const execute = screen.getByRole('button', { name: 'Execute authenticated emergency action' });
  expect(execute).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Emergency reason'), { target: { value: 'Owner requests safe paper flatten' } });
  fireEvent.change(screen.getByLabelText('Type FLATTEN PAPER'), { target: { value: 'FLATTEN PAPER' } });
  fireEvent.click(execute);
  expect(await screen.findByText('WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED')).toBeInTheDocument();
  const submitted = fetcher.mock.calls.find(([url, init]) => url.endsWith('/emergency') && init?.method === 'POST');
  expect(JSON.parse(submitted![1]!.body as string)).toEqual({ action: 'FLATTEN', reason: 'Owner requests safe paper flatten', confirmation: 'FLATTEN PAPER' });
  expect(screen.queryByText('All positions closed')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Positions' }));
  expect(screen.getByText('102')).toBeInTheDocument();
  expect(screen.getByText('RECENT_SOFTWARE_CHECK')).toBeInTheDocument();
  expect(screen.getByText(/not broker-held stop guarantees/)).toBeInTheDocument();
});

test('authenticated dashboard renders backend blockers and truthful missing data', async () => {
  const fixture = {
    trading_mode: 'PAPER', generated_at: '2026-09-20T10:00:00Z',
    new_entries_allowed: false, trading_enabled: false,
    blockers: ['RISK DISARMED — DAILY LOSS LIMIT'], broker_verification: 'GROWW_LIVE_UNVERIFIED',
    account: null, account_status: 'UNAVAILABLE', regime_status: 'STALE_OR_INCOMPLETE',
    regime: { id: 'fixture-regime', underlying: 'FIXTURE-INDEX', as_of: '2026-09-20T09:00:00Z', data_origin: 'SYNTHETIC', label: 'TRENDING_UP', candidate: 'TRENDING_UP', reason: 'INITIAL' },
    positions: [], orders: [], decisions: [], audit: [], strategies: [], chains: [], preflights: [], journal: [],
    components: [{ name: 'scheduler', status: 'NOT_IMPLEMENTED', detail: 'No worker' }],
    notifications: [{ event_id: 'isolated-notice', status: 'RECORDED_CHANNEL_OUTCOMES', channels: { email: 'FAILED' }, external_delivery_verified: false }],
    advisory_calls: [{ id: 'fixture-advisory', provider: 'fallback', outcome: 'FALLBACK_USED', cost_usd: '0', input_tokens: null }],
    advisory_budget: { reserved_usd: '2.020480', accounted_usd: '0.000300', cost_basis: 'OWNER_TARIFF_ESTIMATE_NOT_PROVIDER_INVOICE' },
  };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith('/auth/refresh') ? { access_token: 'test-only-access' } :
      url.endsWith('/reports/paper/daily') ? { reports: [], generated_at: '2026-09-18T10:00:00Z' } : fixture,
  ), { status: 200 })));
  render(<App />);
  expect(await screen.findByText(/ENTRIES BLOCKED/)).toHaveTextContent('DAILY LOSS LIMIT');
  expect(screen.getByText(/Groww LIVE execution/)).toHaveTextContent('UNVERIFIED');
  expect(screen.getByText('TRENDING_UP / STALE_OR_INCOMPLETE')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Positions' }));
  expect(screen.getByText(/No recorded data/)).toBeInTheDocument();
  expect(localStorage.length).toBe(0);
  fireEvent.click(screen.getByRole('button', { name: 'Monitoring' }));
  expect(screen.getByText('isolated-notice')).toBeInTheDocument();
  expect(screen.getByText(/"email":"FAILED"/)).toBeInTheDocument();
  expect(screen.getByText(/not independently verified message delivery/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Decisions' }));
  expect(screen.getByText('fixture-advisory')).toBeInTheDocument();
  expect(screen.getByText('FALLBACK_USED')).toBeInTheDocument();
  expect(screen.getByText(/not a Claude response or order authorization/)).toBeInTheDocument();
  expect(screen.getByText('2.020480')).toBeInTheDocument();
  expect(screen.getByText('OWNER_TARIFF_ESTIMATE_NOT_PROVIDER_INVOICE')).toBeInTheDocument();
});

test('unauthenticated session shows login and does not invent a workspace', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })));
  render(<App />);
  expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  expect(screen.queryByText('System Healthy')).not.toBeInTheDocument();
});

test('expired access refreshes once, retries, and fails closed on revoked session', async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response('{}', { status: 401 }))
    .mockResolvedValueOnce(new Response('{"access_token":"test-only-refreshed"}', { status: 200 }))
    .mockResolvedValueOnce(new Response('{"status":"recorded"}', { status: 200 }))
    .mockResolvedValueOnce(new Response('{}', { status: 401 }))
    .mockResolvedValueOnce(new Response('{}', { status: 401 }));
  vi.stubGlobal('fetch', fetcher);
  const api = new Api();
  await expect(api.request('/workspace')).resolves.toEqual({ status: 'recorded' });
  await expect(api.request('/workspace')).rejects.toMatchObject({ status: 401 });
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
  expect(fetcher.mock.calls[2][1].headers.Authorization).toBe('Bearer test-only-refreshed');
});

test('strategy controls submit owner intent and show server rejection without claiming enablement', async () => {
  const fixture = {
    trading_mode: 'PAPER', generated_at: '2026-09-21T04:30:00Z',
    new_entries_allowed: false, blockers: ['CONTRACT INPUTS UNAVAILABLE'],
    broker_verification: 'GROWW_LIVE_UNVERIFIED', account: null, account_status: 'UNAVAILABLE',
    regime_status: 'UNAVAILABLE', positions: [], orders: [], decisions: [], audit: [], chains: [],
    preflights: [], journal: [], components: [],
    strategies: [{ strategy_id: 'closed-candle-breakout', version: '1', enabled_paper: false, auto_disabled: false }],
  };
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/auth/refresh')) return new Response('{"access_token":"isolated-test-token"}', { status: 200 });
    if (url.endsWith('/closed-candle-breakout/paper')) return new Response('{"detail":"Owner control unavailable"}', { status: 409 });
    if (init?.method === 'POST') return new Response('{"registration_id":"isolated-registration"}', { status: 200 });
    return new Response(JSON.stringify(fixture), { status: 200 });
  });
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await screen.findByText(/ENTRIES BLOCKED/);
  fireEvent.click(screen.getByRole('button', { name: 'Strategies' }));
  const register = screen.getByRole('button', { name: 'Register disabled reference' });
  expect(register).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Strategy change reason'), { target: { value: 'Owner enables isolated hypothesis' } });
  fireEvent.change(screen.getByLabelText('Stored instrument ID'), { target: { value: 'fixture-instrument' } });
  fireEvent.click(register);
  await screen.findByRole('status');
  const call = fetcher.mock.calls.find(([url]) => url.endsWith('/reference/register'));
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({ instrument_id: 'fixture-instrument', kind: 'CASH', reason: 'Owner enables isolated hypothesis' });
  fireEvent.change(screen.getByLabelText('Reference instrument class'), { target: { value: 'LONG_OPTION' } });
  expect(screen.getByText(/PAPER long options require audited/)).toBeInTheDocument();
  fireEvent.click(register);
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/reference/register'))).toHaveLength(2));
  const optionCall = fetcher.mock.calls.filter(([url]) => url.endsWith('/reference/register'))[1];
  expect(JSON.parse(String(optionCall?.[1]?.body)).kind).toBe('LONG_OPTION');
  fireEvent.click(screen.getByRole('button', { name: 'Enable PAPER closed-candle-breakout 1' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Owner control unavailable');
  expect(screen.getByRole('button', { name: 'Enable PAPER closed-candle-breakout 1' })).toBeInTheDocument();
});
