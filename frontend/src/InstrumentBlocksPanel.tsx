import { useCallback, useEffect, useRef, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type State = components['schemas']['InstrumentBlockState'];

export function InstrumentBlocksPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const [rows, setRows] = useState<State[]>();
  const [instrument, setInstrument] = useState('');
  const [review, setReview] = useState<State>();
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const listVersion = useRef(0);
  const reviewVersion = useRef(0);
  const refresh = useCallback(async () => {
    const version = ++listVersion.current;
    try {
      const next = await api.request<State[]>('/risk/instrument-blocks');
      if (version === listVersion.current) setRows(next);
    } catch (failure) {
      if (version === listVersion.current) { setRows(undefined); setError(String(failure)); }
    }
  }, [api]);
  useEffect(() => {
    void refresh(); const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  async function load() {
    const version = ++reviewVersion.current;
    setBusy(true); setError(''); setReview(undefined); setConfirmation('');
    try {
      const next = await api.request<State>(`/risk/instrument-blocks/${encodeURIComponent(instrument)}`);
      if (version === reviewVersion.current) setReview(next);
    } catch (failure) { if (version === reviewVersion.current) setError(String(failure)); }
    finally { setBusy(false); }
  }
  const phrase = review?.manually_blocked ? 'UNBLOCK PAPER INSTRUMENT' : 'BLOCK PAPER INSTRUMENT';
  async function submit() {
    if (!review) return;
    setBusy(true); setError('');
    try {
      const next = await api.request<State>(`/risk/instrument-blocks/${encodeURIComponent(review.instrument_id)}`, {
        method: 'PUT', body: JSON.stringify({ blocked: !review.manually_blocked, expected_event_id: review.event_id, reason, confirmation }),
      });
      setReview(next); setConfirmation(''); await refresh(); onChange();
    } catch (failure) { setReview(undefined); setConfirmation(''); setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Manual instrument controls"><h2>Manual PAPER instrument blocks</h2>
    <p>Blocks apply across all PAPER data origins and never disable exits. Release does not clear news halts, catalog restrictions or other risk gates. Catalog flags are not verified exchange ban-period data.</p>
    {error && <p role="alert">INSTRUMENT CONTROL UNAVAILABLE OR CHANGED — {error}</p>}
    {!rows ? <p>Instrument control list loading or unavailable.</p> : !rows.length ? <p>No recorded manual blocks or catalog restrictions.</p> : rows.map(row => <article key={row.instrument_id}>
      <h3>{row.instrument_id} — {row.manually_blocked ? 'MANUAL ENTRIES BLOCKED' : 'NO MANUAL BLOCK'}</h3>
      <p>Catalog: {row.catalog_restricted ? 'RESTRICTED' : row.catalog_active ? 'Active flag only; other gates apply' : 'INACTIVE'}. Audit: {row.event_id ?? 'No manual event'}</p>
    </article>)}
    <label>Instrument control ID<input disabled={busy} value={instrument} onChange={event => { ++reviewVersion.current; setInstrument(event.target.value); setReview(undefined); setConfirmation(''); }} /></label>
    <button disabled={busy || !instrument} onClick={() => void load()}>Load instrument for review</button>
    {review && <><p>Reviewed version, not silently refreshed. Reload after any concurrent change.</p><pre>{JSON.stringify(review, null, 2)}</pre>
      <label>Instrument control reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <label>Type {phrase}<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || reason.trim().length < 10 || confirmation !== phrase} onClick={() => void submit()}>{review.manually_blocked ? 'Release reviewed manual block' : 'Block reviewed instrument'}</button>
    </>}
  </section>;
}
