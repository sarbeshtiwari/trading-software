import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsInterpretation } from './NewsInterpretation';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('quotation admission uses the displayed server interpretation and instrument', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ event_id: 'interpretation-version', known_at: '2026-01-01T00:00:00Z', current_group_matches: true, result: { status: 'GROUNDED_UNVERIFIED', code: 'SUCCESS', result: { interpretation: { event_type: 'UNKNOWN', direction: 'UNKNOWN', magnitude: 'UNKNOWN', time_horizon: 'UNKNOWN', confidence: '0.5', summary: '', claims: [] }, dropped: [] } } })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsInterpretation api={new Api()} articleId="fixture" groupEventId="group-version" />);
  fireEvent.click(screen.getByRole('button', { name: 'Read saved interpretation' }));
  const input = await screen.findByLabelText('Research instrument ID');
  fireEvent.change(input, { target: { value: 'instrument-fixture' } });
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ status: 'SOURCE_POLICY_ADMITTED', source_id: 'nra_fixture', audit_id: 'admission-audit', known_at: '2026-01-01T00:00:00Z', snapshot: {} })));
  fireEvent.click(screen.getByRole('button', { name: 'Evaluate quotation admission' }));
  expect(await screen.findByText(/SOURCE_POLICY_ADMITTED; evidence nra_fixture/)).toBeInTheDocument();
  expect(fetcher).toHaveBeenLastCalledWith('/api/v1/news/articles/fixture/research', expect.objectContaining({ method: 'POST', body: JSON.stringify({ instrument_id: 'instrument-fixture', interpretation_event_id: 'interpretation-version' }) }));
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Source unavailable' }), { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Evaluate quotation admission' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Source unavailable');
  expect(screen.queryByText(/SOURCE_POLICY_ADMITTED; evidence/)).not.toBeInTheDocument();
});

test('conflicting sources are shown separately and cleared on failed refresh', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ event_id: 'audit', known_at: '2026-01-01T00:00:00Z', assessment: { status: 'CONFLICTING', group_event_id: 'group-version', actionable: false, oppositions: [{ asserted: { publisher_id: 'first', quote: 'Synthetic positive statement' }, negated: { publisher_id: 'second', quote: 'Synthetic opposing statement' } }] } })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsInterpretation api={new Api()} articleId="fixture" groupEventId="group-version" />);
  fireEvent.click(screen.getByRole('button', { name: 'Read source contradictions' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('CONFLICTING');
  expect(screen.getByText('first: Synthetic positive statement')).toBeInTheDocument();
  expect(screen.getByText('second: Synthetic opposing statement')).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Integrity unavailable' }), { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Read source contradictions' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Integrity unavailable');
  expect(screen.queryByText('first: Synthetic positive statement')).not.toBeInTheDocument();
});

test('unavailable provider and stale interpretation never display verified research', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ event_id: 'audit-fixture', known_at: '2026-01-01T00:00:00Z', current_group_matches: false, result: { status: 'UNAVAILABLE', code: 'PROVIDER_UNAVAILABLE', result: null } })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsInterpretation api={new Api()} articleId="fixture" groupEventId="group-version" />);
  fireEvent.click(screen.getByRole('button', { name: 'Generate advisory interpretation' }));
  expect(await screen.findByText(/NEWS INTERPRETATION — UNAVAILABLE/)).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('STALE');
  expect(fetcher).toHaveBeenCalledWith('/api/v1/news/articles/fixture/interpretation', expect.objectContaining({ method: 'POST', body: JSON.stringify({ expected_event_id: 'group-version' }) }));
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Unavailable at timestamp' }), { status: 404 }));
  fireEvent.click(screen.getByRole('button', { name: 'Read saved interpretation' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable at timestamp');
  expect(screen.queryByText(/NEWS INTERPRETATION/)).not.toBeInTheDocument();
});
