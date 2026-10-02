import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Api } from './api';
import { HistoricalJobs } from './HistoricalJobs';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('local research cancellation requires its own explicit reason and confirmation', async () => {
  let cancelled = false;
  const requests: object[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === 'POST') {
      expect(url).toMatch(/historical-jobs\/fixture01\/cancel$/);
      requests.push(JSON.parse(String(init.body))); cancelled = true;
      return new Response(JSON.stringify({ id: 'fixture01', status: 'INTERRUPTED', simulated: true }));
    }
    return new Response(JSON.stringify(url.endsWith('/plans') ? [] : [{
      id: 'fixture01', status: cancelled ? 'INTERRUPTED' : 'RUNNING', progress_pct: '25',
      published: false, liveness: cancelled ? 'TERMINAL' : 'CONFIRMED_LOCAL', cancellable: !cancelled,
      error_code: cancelled ? 'OWNER_CANCELLED' : null,
    }]));
  }));
  render(<HistoricalJobs api={new Api()} />);
  fireEvent.click(await screen.findByRole('button', { name: /^Cancel fixture01$/ }));
  const confirm = screen.getByRole('button', { name: 'Confirm research cancellation' });
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'Owner stops this research' } });
  fireEvent.change(screen.getByLabelText('Type CANCEL HISTORICAL RESEARCH'), { target: { value: 'CANCEL HISTORICAL RESEARCH' } });
  fireEvent.click(confirm);
  expect(await screen.findByText(/OWNER_CANCELLED/)).toHaveTextContent('REPORT NOT PUBLISHED');
  expect(requests).toEqual([{ reason: 'Owner stops this research', confirmation: 'CANCEL HISTORICAL RESEARCH' }]);
  expect(screen.queryByRole('button', { name: /^Cancel fixture01$/ })).not.toBeInTheDocument();
});

test('owner launch requires confirmation and transmits only a plan identity and reason', async () => {
  const requests: object[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    if (init.method === 'POST') {
      requests.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ detail: 'Historical job slot reserved; inspect existing jobs' }), { status: 409 });
    }
    return new Response(JSON.stringify(url.endsWith('/plans') ? [{ id: 'fixture01', label: 'Owner fixture plan' }] : [{
      id: 'orphan01', status: 'RUNNING', progress_pct: '25', published: false, liveness: 'UNVERIFIED_OWNER_REVIEW_REQUIRED',
    }]), { status: 200 });
  }));
  render(<HistoricalJobs api={new Api()} />);
  await screen.findByRole('option', { name: 'Owner fixture plan' });
  const button = screen.getByRole('button', { name: 'Launch isolated PAPER research' });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Historical plan'), { target: { value: 'fixture01' } });
  fireEvent.change(screen.getByLabelText('Research reason'), { target: { value: 'Owner research request' } });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Type RUN HISTORICAL PAPER'), { target: { value: 'RUN HISTORICAL PAPER' } });
  fireEvent.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('slot reserved');
  expect(requests).toEqual([{ plan_id: 'fixture01', reason: 'Owner research request', confirmation: 'RUN HISTORICAL PAPER' }]);
  expect(screen.getByText(/UNVERIFIED_OWNER_REVIEW_REQUIRED/)).toHaveTextContent('REPORT NOT PUBLISHED');
});
