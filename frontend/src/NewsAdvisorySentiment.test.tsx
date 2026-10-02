import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { NewsAdvisorySentiment } from './NewsAdvisorySentiment';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('advisory state comes from API and failed refresh removes the old score', async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({ status: 'AVAILABLE', code: 'NEWS_SENTIMENT_AVAILABLE', result: { score: '0.25' }, as_of: '2026-01-01T00:00:00Z', data_origin: 'SYNTHETIC' })));
  vi.stubGlobal('fetch', fetcher);
  render(<NewsAdvisorySentiment api={new Api()} />);
  expect(screen.getByText(/Model confidence is uncalibrated/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Sentiment instrument ID'), { target: { value: 'fixture' } });
  fireEvent.change(screen.getByLabelText('Sentiment data origin'), { target: { value: 'SYNTHETIC' } });
  fireEvent.click(screen.getByRole('button', { name: 'Resolve advisory sentiment' }));
  expect(await screen.findByText(/Score: 0.25/)).toBeInTheDocument();
  expect(String(fetcher.mock.calls[0]?.[0])).toContain('/news/sentiment/fixture?origin=SYNTHETIC');
  fetcher.mockImplementation(async () => new Response(JSON.stringify({ detail: 'Service unavailable' }), { status: 503 }));
  fireEvent.click(screen.getByRole('button', { name: 'Resolve advisory sentiment' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('SENTIMENT UNAVAILABLE');
  expect(screen.queryByText(/Score: 0.25/)).not.toBeInTheDocument();
});
