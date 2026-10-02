import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsArticleImport } from './NewsArticleImport';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('story grouping remains unverified and clears stale results on failure', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ article_id: 'fixture', audit_id: 'audit', observed_at: '2026-01-01T00:00:00Z', acquisition: 'OWNER_IMPORT', verification_status: 'UNVERIFIED' })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsArticleImport api={new Api()} slug="fixture" version="version" />);
  fireEvent.change(screen.getByLabelText('Article JSON'), { target: { value: '{}' } });
  fireEvent.click(screen.getByRole('button', { name: 'Import article observation' }));
  const read = await screen.findByRole('button', { name: 'Read story grouping' });
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ group_id: 'story-fixture', event_id: 'audit-fixture', known_at: '2026-01-01T00:00:00Z', story: { verification_status: 'UNVERIFIED', members: [], ambiguous_group_match: true } })));
  fireEvent.click(read);
  expect(await screen.findByText('Story group — UNVERIFIED')).toBeInTheDocument();
  expect(screen.getByText(/Similarity is not factual corroboration/)).toBeInTheDocument();
  expect(screen.getByText('Ambiguous group match: kept separate.')).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Integrity unavailable' }), { status: 409 }));
  fireEvent.click(read);
  expect(await screen.findByRole('alert')).toHaveTextContent('Story grouping unavailable');
  expect(screen.queryByText('Story group — UNVERIFIED')).not.toBeInTheDocument();
});

test('reads attributed mentions and clears them on a failed refresh', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ article_id: 'fixture', audit_id: 'audit', observed_at: '2026-01-01T00:00:00Z', acquisition: 'OWNER_IMPORT', verification_status: 'UNVERIFIED' })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsArticleImport api={new Api()} slug="fixture" version="version" />);
  fireEvent.change(screen.getByLabelText('Article JSON'), { target: { value: '{}' } });
  fireEvent.click(screen.getByRole('button', { name: 'Import article observation' }));
  const read = await screen.findByRole('button', { name: 'Read instrument mentions' });
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ event_id: 'entity-audit', known_at: '2026-01-01T00:00:00Z', attribution: { matches: [{ anchor: 'NSE:TEST', instrument_ids: ['instrument-test'], rule: 'QUALIFIED_SYMBOL', confidence: '1.0' }], excluded: [] } })));
  fireEvent.click(read);
  expect(await screen.findByText(/NSE:TEST: instrument-test/)).toBeInTheDocument();
  expect(screen.getByText(/Rule scores are not probabilities/)).toBeInTheDocument();
  expect(fetcher).toHaveBeenLastCalledWith('/api/v1/news/articles/fixture/entities', expect.any(Object));
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Integrity unavailable' }), { status: 409 }));
  fireEvent.click(read);
  expect(await screen.findByRole('alert')).toHaveTextContent('Attribution unavailable');
  expect(screen.queryByText(/NSE:TEST: instrument-test/)).not.toBeInTheDocument();
});

test('article import displays only actual receipt and never claims verification', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ article_id: 'fixture-article', audit_id: 'fixture-audit', observed_at: '2026-01-01T00:00:00Z', acquisition: 'OWNER_IMPORT', verification_status: 'UNVERIFIED' })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsArticleImport api={new Api()} slug="fixture" version="source-audit" />);
  fireEvent.change(screen.getByLabelText('Article JSON'), { target: { value: '{"body":"Synthetic test only"}' } });
  fireEvent.click(screen.getByRole('button', { name: 'Import article observation' }));
  expect(await screen.findByText(/OWNER_IMPORT — UNVERIFIED/)).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Source disabled' }), { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Import article observation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Source disabled');
  expect(screen.queryByText(/OWNER_IMPORT — UNVERIFIED/)).not.toBeInTheDocument();
});
