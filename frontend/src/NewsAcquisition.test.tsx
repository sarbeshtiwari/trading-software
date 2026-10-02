import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsAcquisition } from './NewsAcquisition';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('actual API degradation and rate-limit errors cannot become connected state', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ attempt_id: 'fixture-attempt', status: 'DEGRADED', articles: [] })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsAcquisition api={new Api()} slug="fixture" version="audit-fixture" />);
  fireEvent.click(screen.getByRole('button', { name: 'Fetch configured news source' }));
  expect(await screen.findByText('NEWS SERVICE DEGRADED')).toBeInTheDocument();
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Rate limited' }), { status: 409 }));
  fireEvent.click(screen.getByRole('button', { name: 'Fetch configured news source' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Rate limited');
  expect(screen.queryByText('NEWS SERVICE DEGRADED')).not.toBeInTheDocument();
});
