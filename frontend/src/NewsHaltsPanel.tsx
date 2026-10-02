import { useCallback, useEffect, useRef, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Halt = components['schemas']['NewsHaltState'];
type Policy = components['schemas']['ReactionPolicyView'];

export function NewsHaltsPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const [halts, setHalts] = useState<Halt[]>();
  const [policy, setPolicy] = useState<Policy>();
  const [configuration, setConfiguration] = useState('');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const observedHeads = useRef('');
  const requestVersion = useRef(0);
  const refresh = useCallback(async (loadPolicy = false) => {
    const version = ++requestVersion.current;
    try {
      const [next, current] = await Promise.all([
        api.request<Halt[]>('/risk/news-halts'), loadPolicy ? api.request<Policy>('/risk/news-policy') : Promise.resolve(undefined),
      ]);
      if (version !== requestVersion.current) return;
      const heads = JSON.stringify(next.map(halt => [halt.instrument_id, halt.origin, halt.event_id]).sort());
      if (heads !== observedHeads.current) setConfirmation('');
      observedHeads.current = heads;
      setHalts(next); if (current) setPolicy(current); setError('');
    } catch (failure) {
      if (version !== requestVersion.current) return;
      setHalts(undefined); setConfirmation('');
      if (loadPolicy) setPolicy(undefined);
      setError(String(failure));
    }
  }, [api]);
  useEffect(() => {
    void refresh(true); const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  async function release(halt: Halt) {
    setBusy(true); setError('');
    try {
      await api.request(`/risk/news-halts/${encodeURIComponent(halt.instrument_id)}/release`, {
        method: 'POST', body: JSON.stringify({ origin: halt.origin, expected_event_id: halt.event_id, reason, confirmation }),
      });
      setConfirmation(''); await refresh(); onChange();
    } catch (failure) { setError(String(failure)); } finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError('');
    try {
      await api.request('/risk/news-policy', { method: 'PUT', body: JSON.stringify({
        policy: JSON.parse(configuration), expected_event_id: policy?.event_id ?? null, reason,
      }) });
      await refresh(true); onChange();
    } catch (failure) { setError(String(failure)); } finally { setBusy(false); }
  }
  return <section aria-label="News entry halts"><h2>Instrument news entry halts</h2>
    <p>Entry inhibition only; exits remain enabled. Source-admitted quotations and uncalibrated model labels are not verified facts. A release acknowledges existing evidence; new evidence may halt entries again. Other risk latches are unchanged.</p>
    {error && <p role="alert">NEWS HALT STATE UNAVAILABLE — {error}</p>}
    {!halts ? <p>Loading or unavailable news halt state.</p> : !halts.length ? <p>No recorded news halts.</p> : halts.map(halt => <article key={`${halt.instrument_id}:${halt.origin}`}>
      <h3>{halt.instrument_id} — {halt.blocked ? 'ENTRIES BLOCKED — NEWS HALT' : 'NEWS HALT RELEASED'}</h3>
      <p>{halt.origin} · Audit {halt.event_id} · {halt.known_at}</p>
      <details><summary>Source and held-position evidence</summary><pre>{JSON.stringify(halt.causes, null, 2)}</pre></details>
      {halt.blocked && <button disabled={busy || reason.trim().length < 10 || confirmation !== 'REVIEWED NEWS HALT'} onClick={() => void release(halt)}>Release reviewed halt for {halt.instrument_id}</button>}
    </article>)}
    <label>News halt review reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <label>Type REVIEWED NEWS HALT<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
    <details><summary>Owner news reaction policy</summary>
      <p>{policy ? policy.policy?.enabled ? 'REACTION POLICY ENABLED' : 'REACTION POLICY DISABLED / UNCONFIGURED' : 'POLICY UNAVAILABLE'}. Disabling does not clear existing halts.</p>
      <pre>{JSON.stringify(policy, null, 2)}</pre>
      <p>Policy edits use the displayed audit version; polling never silently advances that version.</p>
      <button disabled={busy} onClick={() => { setConfiguration(''); setConfirmation(''); void refresh(true); }}>Reload reviewed news policy</button>
      <p>Supply enabled, accept_uncalibrated_labels, minimum_confidence (0–1), and max_age_seconds (1–86400). No thresholds or consent are assumed.</p>
      <textarea aria-label="News reaction policy JSON" value={configuration} onChange={event => setConfiguration(event.target.value)} />
      <button disabled={busy || !policy || !configuration || reason.trim().length < 10} onClick={() => void save()}>Save audited news reaction policy</button>
    </details>
  </section>;
}
