import { useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Job = components['schemas']['ProductionView'];
type Action = components['schemas']['ProductionControl']['action'];

export function NewsProductionControls({ api }: { api: Api }) {
  const [group, setGroup] = useState('');
  const [view, setView] = useState<Job>();
  const [reason, setReason] = useState('');
  const [seconds, setSeconds] = useState('300');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function load() {
    setBusy(true); setError(''); setView(undefined);
    try { setView(await api.request<Job>(`/news/production/${encodeURIComponent(group)}`)); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function act(action: Action) {
    if (!view) return;
    const expected = view.event_id;
    setBusy(true); setError(''); setView(undefined);
    try {
      setView(await api.request<Job>(`/news/production/${encodeURIComponent(group)}/control`, {
        method: 'POST', body: JSON.stringify({ action, expected_event_id: expected, reason, authorization_seconds: Number(seconds) }),
      }));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  const blocked = busy || !view || reason.trim().length < 10;
  return <section><h3>Research job recovery</h3>
    <p>Recover uses saved receipts only. Retry requires a proven no-call failure and still passes the shared budget gates. Pending or ambiguous paid calls cannot be retried here.</p>
    <p>Abandon stops this automatic job, not already-sent requests. It neither reconciles billing nor revokes existing evidence or risk controls.</p>
    <label>Research group event ID<input maxLength={40} disabled={busy} value={group} onChange={event => { setGroup(event.target.value); setView(undefined); setError(''); }} /></label>
    <button disabled={busy || !group} onClick={() => void load()}>Load research job</button>
    {busy && <p role="status">Loading research job state…</p>}
    {error && <p role="alert">RESEARCH CONTROL UNAVAILABLE — {error}. Reload the current job before acting.</p>}
    {view && <><p>Research job state: {view.state}. Attempts: {view.attempts}; retry authorizations: {view.retry_authorizations}.</p>
      <p>Outcome: {view.result?.code ?? 'UNKNOWN — no completion receipt'}. Billing: {view.billing_verification}. Version: {view.event_id}.</p>
      <p>Retry authorization expires: {view.authorized_until ?? 'NONE'}. Authorization alone does not start a provider request; the opted-in scheduler must consume it.</p></>}
    <label>Research recovery reason<input maxLength={500} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label>
    <label>Research retry authorization seconds<input type="number" min={1} max={900} disabled={busy} value={seconds} onChange={event => setSeconds(event.target.value)} /></label>
    <button disabled={blocked || view?.state !== 'PENDING'} onClick={() => void act('RECOVER')}>Recover saved research receipt</button>
    <button disabled={blocked || !['FINISHED', 'RETRY_EXPIRED'].includes(view?.state ?? '') || view?.result?.status !== 'UNAVAILABLE'} onClick={() => void act('AUTHORIZE_RETRY')}>Authorize one research retry</button>
    <button disabled={blocked || view?.state === 'ABANDONED'} onClick={() => void act('ABANDON')}>Abandon automatic research job</button>
  </section>;
}
