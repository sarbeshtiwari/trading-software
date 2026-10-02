import { useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type State = components['schemas']['BanState'];

export function FnoBanPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const [origin, setOrigin] = useState('');
  const [review, setReview] = useState<State>();
  const [document, setDocument] = useState('');
  const [knownAt, setKnownAt] = useState('');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function load() {
    setBusy(true); setReview(undefined); setConfirmation(''); setError('');
    try { setReview(await api.request<State>(`/risk/fno-bans?origin=${encodeURIComponent(origin)}`)); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function submit() {
    if (!review) return;
    setBusy(true); setError('');
    try {
      setReview(await api.request<State>('/risk/fno-bans', { method: 'PUT', body: JSON.stringify({
        origin: review.origin, document, known_at: knownAt, reason, confirmation, expected_event_id: review.head_id,
      }) }));
      setDocument(''); setKnownAt(''); setConfirmation(''); onChange();
    } catch (failure) { setReview(undefined); setConfirmation(''); setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="F&O ban report controls"><h2>NSE F&O ban reports</h2>
    <p>Owner-admitted source documents, not a verified automatic feed. Missing current-session evidence blocks F&O entries. These controls do not enable the currently disabled F&O OMS. CASH trading and protective exits are not blocked by this list.</p>
    {error && <p role="alert">BAN REPORT UNAVAILABLE OR CHANGED — {error}</p>}
    <label>Ban report origin<select aria-label="Ban report origin" disabled={busy} value={origin} onChange={event => { setOrigin(event.target.value); setReview(undefined); setConfirmation(''); }}>
      <option value="">Select explicitly</option>{['LIVE', 'HISTORICAL', 'REPLAY', 'SYNTHETIC'].map(value => <option key={value}>{value}</option>)}
    </select></label>
    <button disabled={busy || !origin} onClick={() => void load()}>Review ban report state</button>
    {review && <><pre>{JSON.stringify(review, null, 2)}</pre>
      <p>Reviewed state as of {review.as_of}. Reload before admission if another publisher changed the report. A report for tomorrow does not clear today's ban.</p>
      <label>NSE ban CSV<textarea aria-label="NSE ban CSV" disabled={busy} value={document} onChange={event => setDocument(event.target.value)} /></label>
      <label>Report known at (ISO timestamp with timezone)<input disabled={busy} value={knownAt} onChange={event => setKnownAt(event.target.value)} /></label>
      <label>Ban report review reason<input disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <label>Type ADMIT PAPER NSE BAN REPORT<input disabled={busy} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || !document || !knownAt || reason.trim().length < 10 || confirmation !== 'ADMIT PAPER NSE BAN REPORT'} onClick={() => void submit()}>Admit reviewed ban report</button>
    </>}
  </section>;
}
