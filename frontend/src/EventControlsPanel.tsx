import { useCallback, useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type State = components['schemas']['EventControlState'];
type Change = components['schemas']['EventControlChange'];

export function EventControlsPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const [rows, setRows] = useState<State[]>();
  const [instrument, setInstrument] = useState('');
  const [origin, setOrigin] = useState('');
  const [review, setReview] = useState<State>();
  const [document, setDocument] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try { setRows(await api.request<State[]>('/risk/event-controls')); }
    catch (failure) { setRows(undefined); setError(String(failure)); }
  }, [api]);
  useEffect(() => {
    void refresh(); const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  function invalidate() { setReview(undefined); setDocument(''); setConfirmation(''); }
  async function load() {
    setBusy(true); setError(''); invalidate();
    try {
      const next = await api.request<State>(`/risk/event-controls/${encodeURIComponent(instrument)}?origin=${encodeURIComponent(origin)}`);
      setReview(next);
      if (next.control) setDocument(JSON.stringify({ ...next.control, confirmation: '' }, null, 2));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  let parsed: Change | undefined;
  try { parsed = JSON.parse(document) as Change; } catch { parsed = undefined; }
  const phrase = parsed?.enabled === false ? 'DISABLE PAPER EVENT CONTROL' : 'PUBLISH PAPER EVENT CONTROL';
  async function submit() {
    if (!review || !parsed) return;
    setBusy(true); setError('');
    try {
      const next = await api.request<State>(`/risk/event-controls/${encodeURIComponent(review.instrument_id)}`, {
        method: 'PUT', body: JSON.stringify({ ...parsed, origin: review.origin, expected_event_id: review.event_id, confirmation }),
      });
      setReview(next); setConfirmation(''); await refresh(); onChange();
    } catch (failure) { invalidate(); setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Event calendar controls"><h2>PAPER event calendar controls</h2>
    <p>Owner-published evidence, not an externally verified feed. Controls apply to the selected instrument and origin. Blackouts and unavailable configured calendars block entries, never protective exits. Unconfigured does not mean no events.</p>
    {error && <p role="alert">EVENT CONTROL UNAVAILABLE OR CHANGED — {error}</p>}
    {!rows ? <p>Event controls loading or unavailable.</p> : !rows.length ? <p>No published event controls.</p> : rows.map(row => <article key={`${row.instrument_id}:${row.origin}`}>
      <h3>{row.instrument_id} / {row.origin} — {row.status}</h3><p>Events: {row.event_ids.join(', ') || 'None reported'}. Audit: {row.event_id}</p>
    </article>)}
    <label>Event instrument ID<input disabled={busy} value={instrument} onChange={event => { setInstrument(event.target.value); invalidate(); }} /></label>
    <label>Event data origin<select aria-label="Event data origin" disabled={busy} value={origin} onChange={event => { setOrigin(event.target.value); invalidate(); }}>
      <option value="">Select explicitly</option><option value="LIVE">LIVE</option><option value="HISTORICAL">HISTORICAL</option><option value="REPLAY">REPLAY</option><option value="SYNTHETIC">SYNTHETIC</option>
    </select></label>
    <button disabled={busy || !instrument || !origin} onClick={() => void load()}>Review event control</button>
    {review && <><p>Reviewed version: {review.event_id ?? 'UNCONFIGURED'}. Polling does not replace this reviewed version.</p>
      <pre>{JSON.stringify(review, null, 2)}</pre>
      <p>Supply EventControlChange JSON: explicit freshness limit, source/knowledge/coverage dates, events or corporate evidence with blackout windows, strategy scope, enabled flag and review reason. No values are invented. Origin and version come from this review.</p>
      <label>Event control JSON<textarea aria-label="Event control JSON" disabled={busy} value={document} onChange={event => { setDocument(event.target.value); setConfirmation(''); }} /></label>
      <label>Type {phrase}<input disabled={busy} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || !parsed || confirmation !== phrase} onClick={() => void submit()}>Publish reviewed event control</button>
    </>}
  </section>;
}
