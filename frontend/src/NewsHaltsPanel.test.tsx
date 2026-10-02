import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsHaltsPanel } from './NewsHaltsPanel';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

test('news halt release is explicit, versioned and never optimistic', async () => {
  const fetcher = vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === 'POST') return new Response(JSON.stringify({ detail: 'Halt changed' }), { status: 409 });
    return new Response(JSON.stringify(String(url).endsWith('news-policy') ? { event_id: null, policy: null } : [{
      instrument_id: 'fixture-instrument', origin: 'SYNTHETIC', event_id: 'halt-head', blocked: true,
      known_at: '2026-01-01T04:00:00Z', causes: [{ admission_id: 'source-evidence' }], acknowledged: ['source-evidence'],
    }]));
  });
  vi.stubGlobal('fetch', fetcher);
  render(<NewsHaltsPanel api={new Api()} onChange={() => undefined} />);
  expect(await screen.findByText(/ENTRIES BLOCKED — NEWS HALT/)).toBeInTheDocument();
  const button = screen.getByRole('button', { name: 'Release reviewed halt for fixture-instrument' });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText('News halt review reason'), { target: { value: 'Owner reviews fixture evidence' } });
  fireEvent.change(screen.getByLabelText('Type REVIEWED NEWS HALT'), { target: { value: 'REVIEWED NEWS HALT' } });
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('Halt changed');
  expect(screen.getByText(/ENTRIES BLOCKED — NEWS HALT/)).toBeInTheDocument();
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'POST');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ expected_event_id: 'halt-head', origin: 'SYNTHETIC' });
});

test('unavailable news halt reads do not report healthy or fabricate empty state', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 503 })));
  render(<NewsHaltsPanel api={new Api()} onChange={() => undefined} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('NEWS HALT STATE UNAVAILABLE');
  expect(screen.queryByText('No recorded news halts.')).not.toBeInTheDocument();
});

test('polling invalidates changed halt review without advancing the edited policy version', async () => {
  vi.useFakeTimers();
  let head = 'first-halt';
  let policyHead = 'first-policy';
  const fetcher = vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === 'PUT') return new Response(JSON.stringify({ detail: 'Policy changed' }), { status: 409 });
    return new Response(JSON.stringify(String(url).endsWith('news-policy') ? { event_id: policyHead, policy: null } : [{
      instrument_id: 'fixture', origin: 'SYNTHETIC', event_id: head, blocked: true,
      known_at: '2026-01-01T04:00:00Z', causes: [], acknowledged: [],
    }]));
  });
  vi.stubGlobal('fetch', fetcher);
  await act(async () => { render(<NewsHaltsPanel api={new Api()} onChange={() => undefined} />); });
  fireEvent.change(screen.getByLabelText('News halt review reason'), { target: { value: 'Owner reviewed first event only' } });
  fireEvent.change(screen.getByLabelText('Type REVIEWED NEWS HALT'), { target: { value: 'REVIEWED NEWS HALT' } });
  fireEvent.change(screen.getByLabelText('News reaction policy JSON'), { target: { value: '{"enabled":false}' } });
  expect(screen.getByRole('button', { name: 'Release reviewed halt for fixture' })).toBeEnabled();
  head = 'second-halt'; policyHead = 'second-policy';
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(screen.getByLabelText('Type REVIEWED NEWS HALT')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Release reviewed halt for fixture' })).toBeDisabled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save audited news reaction policy' })); });
  const request = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ expected_event_id: 'first-policy' });
  expect(screen.getByRole('alert')).toHaveTextContent('Policy changed');
});
