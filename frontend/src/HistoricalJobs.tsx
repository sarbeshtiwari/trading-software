import { useEffect, useState } from 'react';
import type { components } from './api-schema';
import { Api } from './api';

type Plan = components['schemas']['PlanView'];
type Job = components['schemas']['JobView'];

export function HistoricalJobs({ api, experiment = false }: { api: Api; experiment?: boolean }) {
  const resource = experiment ? '/historical-jobs/experiments' : '/historical-jobs';
  const phrase = experiment ? 'RUN WALK FORWARD' : 'RUN HISTORICAL PAPER';
  const selector = experiment ? 'walkforward-plan' : 'historical-plan';
  const [plans, setPlans] = useState<Plan[]>();
  const [jobs, setJobs] = useState<Job[]>();
  const [selected, setSelected] = useState('');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [cancelId, setCancelId] = useState('');
  const [cancelReason, setCancelReason] = useState('');
  const [cancelConfirmation, setCancelConfirmation] = useState('');
  useEffect(() => {
    let active = true, pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const [available, current] = await Promise.all([
          api.request<Plan[]>(`${resource}/plans`), api.request<Job[]>(resource),
        ]);
        if (active) { setPlans(available); setJobs(current); setError(''); }
      } catch (failure) { if (active) setError(String(failure)); }
      finally { pending = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, revision, resource]);
  async function launch() {
    setBusy(true); setError('');
    try {
      await api.request(resource, { method: 'POST', body: JSON.stringify({ plan_id: selected, reason, confirmation }) });
      setConfirmation(''); setRevision(value => value + 1);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function cancel() {
    setBusy(true); setError('');
    try {
      await api.request(`${resource}/${encodeURIComponent(cancelId)}/cancel`, {
        method: 'POST', body: JSON.stringify({ reason: cancelReason, confirmation: cancelConfirmation }),
      });
      setCancelId(''); setCancelConfirmation(''); setRevision(value => value + 1);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label={experiment ? 'Walk-forward controls' : 'Historical job controls'}><h2>{experiment ? 'Chronological walk-forward experiments' : 'Owner-configured historical jobs'}</h2>
    <p>Only server-owned plans can run. No database URL or file path is accepted. One isolated job at a time; interrupted jobs are not automatically replayed.</p>
    {!plans && !error && <p role="status">Loading configured plans…</p>}
    {plans?.length === 0 && <p>Historical launch unavailable: no owner plans configured.</p>}
    {error && <p role="alert">HISTORICAL JOB STATE UNAVAILABLE — {error}. Displayed state may be stale.</p>}
    {!!plans?.length && <form onSubmit={event => { event.preventDefault(); void launch(); }}>
      <label htmlFor={selector}>{experiment ? 'Walk-forward experiment' : 'Historical plan'}</label><select id={selector} value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select an owner plan</option>{plans.map(plan => <option key={plan.id} value={plan.id}>{plan.label}</option>)}</select>
      <label>Research reason<input value={reason} minLength={10} maxLength={500} onChange={event => setReason(event.target.value)} /></label>
      <label>Type {phrase}<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || !selected || reason.trim().length < 10 || confirmation !== phrase}>{experiment ? 'Launch walk-forward research' : 'Launch isolated PAPER research'}</button>
    </form>}
    {jobs?.length === 0 && <p>No historical jobs requested.</p>}
    {jobs?.map(job => <div key={job.id}><p>{job.id}: {job.status} / {job.progress_pct}% / {job.liveness} / {job.published ? 'REPORT PUBLISHED' : 'REPORT NOT PUBLISHED'}{job.error_code ? ` / ${job.error_code}` : ''}</p>
      {job.cancellable && <button disabled={busy || !!error} onClick={() => { setCancelId(job.id); setCancelConfirmation(''); }}>Cancel {job.id}</button>}
    </div>)}
    {cancelId && <form aria-label="Cancel historical research" onSubmit={event => { event.preventDefault(); void cancel(); }}>
      <p>Cancel {cancelId}. The controller waits for its local child to stop. Unknown-owner jobs cannot be cancelled here; interrupted research is not a completed report.</p>
      <label>Cancellation reason<input value={cancelReason} minLength={10} maxLength={500} onChange={event => setCancelReason(event.target.value)} /></label>
      <label>Type CANCEL HISTORICAL RESEARCH<input value={cancelConfirmation} onChange={event => setCancelConfirmation(event.target.value)} /></label>
      <button disabled={busy || !!error || cancelReason.trim().length < 10 || cancelConfirmation !== 'CANCEL HISTORICAL RESEARCH'}>Confirm research cancellation</button>
    </form>}
  </section>;
}
