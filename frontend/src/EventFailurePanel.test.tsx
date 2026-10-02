import { render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { EventFailurePanel } from './EventFailurePanel';

afterEach(() => vi.unstubAllGlobals());

test('retained failure metadata is visible without payload or healthy claim', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ status: 'RETAINED_FAILURES', retained_count: 1, failures: [{ receipt_id: '1-0', event_type: 'execution.fill', entry_id: '0-1', handler_reference: 'digest', category: 'HANDLER_FAILURE' }], has_more: false, scope: 'Retention is not external delivery.' }))));
  render(<EventFailurePanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('RETAINED_FAILURES');
  expect(screen.getByRole('cell', { name: 'execution.fill' })).toBeInTheDocument();
});

test('dependency failure is not represented as zero retained events', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ status: 'EVENT_MONITOR_UNAVAILABLE', retained_count: null, failures: [] }))));
  render(<EventFailurePanel api={new Api()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('EVENT_MONITOR_UNAVAILABLE');
  expect(screen.getByText('Retained records: UNAVAILABLE')).toBeInTheDocument();
});
