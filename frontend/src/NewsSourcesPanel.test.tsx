import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsSourcesPanel } from './NewsSourcesPanel';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('runtime research recovery is visible and failed refresh removes the old batch', async () => {
  const fetcher = vi.fn(async (url: unknown) => new Response(JSON.stringify(String(url).endsWith('/runtime') ? {
    status: 'DEGRADED', actionable_news: 'PER_INSTRUMENT_CHECK_REQUIRED', research_enabled: true,
    research_status: 'DEGRADED', research_results: [{ article_id: 'fixture-story', group_event_id: 'fixture-group', status: 'RECOVERY_REQUIRED', code: 'RESEARCH_OUTCOME_UNKNOWN', admission_ids: [] }],
  } : { sources: [], has_more: false })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsSourcesPanel api={new Api()} />);
  expect(await screen.findByText(/Research fixture-story: RECOVERY_REQUIRED/)).toBeInTheDocument();
  expect(screen.getByText(/Research worker: DEGRADED/)).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh source configuration' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText(/Research fixture-story: RECOVERY_REQUIRED/)).not.toBeInTheDocument();
});

test('empty and failed source configuration never claims news connectivity', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ sources: [], has_more: false })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsSourcesPanel api={new Api()} />);
  expect(await screen.findByText('No configured sources. No news has been invented.')).toBeInTheDocument();
  expect(screen.getByText(/Automatic polling requires server opt-in/)).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 }));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh source configuration' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('may be stale');
});

test('editing uses the current audit version and keeps failed changes unconfirmed', async () => {
  const source = { slug: 'fixture', event_id: 'audit-1', integrity: 'AUDITED', acquisition_status: 'SUPPORTED_ADAPTER', configuration: {
    name: 'Fixture source', publisher_id: 'fixture', tier: 2, kind: 'rss', endpoint: 'https://fixture.example.test/feed', weight: '1', is_enabled: false,
  } };
  const fetcher = vi.fn(async (_url: unknown, init?: RequestInit) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'Concurrent update' }), { status: 409 })
    : new Response(JSON.stringify({ sources: [source], has_more: false })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsSourcesPanel api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'fixture' }));
  fireEvent.change(screen.getByLabelText('Source configuration reason'), { target: { value: 'Owner reviews fixture policy' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save source configuration' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === 'PUT' && JSON.parse(String(init.body)).expected_event_id === 'audit-1')).toBe(true));
  expect(await screen.findByRole('alert')).toHaveTextContent('Concurrent update');
  expect(screen.queryByText(/Configuration recorded:/)).not.toBeInTheDocument();
});
