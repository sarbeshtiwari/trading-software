import { useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

export function NewsInterpretation({ api, articleId, groupEventId }: { api: Api; articleId: string; groupEventId: string }) {
  const [view, setView] = useState<components['schemas']['InterpretationView']>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [instrument, setInstrument] = useState('');
  const [admission, setAdmission] = useState<components['schemas']['AdmissionView']>();
  async function admit() {
    if (!view) return;
    setBusy(true); setError(''); setAdmission(undefined);
    try {
      setAdmission(await api.request<components['schemas']['AdmissionView']>(`/news/articles/${encodeURIComponent(articleId)}/research`, {
        method: 'POST', body: JSON.stringify({ instrument_id: instrument, interpretation_event_id: view.event_id }),
      }));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  const [conflict, setConflict] = useState<components['schemas']['ConflictView']>();
  async function loadConflicts() {
    setBusy(true); setError(''); setConflict(undefined);
    try {
      setConflict(await api.request<components['schemas']['ConflictView']>(`/news/articles/${encodeURIComponent(articleId)}/conflicts`));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function load(generate: boolean) {
    setBusy(true); setError(''); setView(undefined); setAdmission(undefined);
    try {
      setView(await api.request<components['schemas']['InterpretationView']>(`/news/articles/${encodeURIComponent(articleId)}/interpretation`, generate ? {
        method: 'POST', body: JSON.stringify({ expected_event_id: groupEventId }),
      } : {}));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  const result = view?.result.result;
  const interpretation = result?.interpretation;
  return <section aria-label="Advisory news interpretation">
    <h4>Advisory extraction — never trading authorization</h4>
    <p>Generating may use the configured Claude account within its budget. No provider means UNAVAILABLE, not invented research.</p>
    <button disabled={busy} onClick={() => load(false)}>Read saved interpretation</button>
    <button disabled={busy} onClick={() => load(true)}>Generate advisory interpretation</button>
    <button disabled={busy} onClick={loadConflicts}>Read source contradictions</button>
    {busy && <p role="status">Loading interpretation…</p>}
    {error && <p role="alert">Interpretation unavailable: {error}</p>}
    {conflict && <section aria-label="Source contradiction assessment">
      <p role={conflict.assessment.status === 'CONFLICTING' ? 'alert' : 'status'}>NEWS CONFLICT ASSESSMENT — {conflict.assessment.status}</p>
      <p>Lexical heuristic warning, not fact verification. UNASSESSED does not mean agreement. Conflicting statements block interpretation.</p>
      <p>Audit {conflict.event_id}; known at {conflict.known_at}.</p>
      {conflict.assessment.group_event_id !== groupEventId && <p role="alert">STALE VIEW — reload story grouping.</p>}
      {conflict.assessment.oppositions.map((pair, index) => <div key={index}>
        <blockquote>{pair.asserted.publisher_id}: {pair.asserted.quote}</blockquote>
        <blockquote>{pair.negated.publisher_id}: {pair.negated.quote}</blockquote>
      </div>)}
    </section>}
    {view && <>
      <p role="status">NEWS INTERPRETATION — {view.result.status}; {view.result.code}</p>
      <p>Audit {view.event_id}; known at {view.known_at}.</p>
      {!view.current_group_matches && <p role="alert">STALE — story grouping has changed.</p>}
      {result && <p>UNVERIFIED. Confidence is an uncalibrated model self-report. Source quotations do not verify model annotations or factual truth.</p>}
      {interpretation && <>
        <p>{interpretation.event_type}; {interpretation.direction}; magnitude {interpretation.magnitude}; horizon {interpretation.time_horizon}; model score {interpretation.confidence}</p>
        {interpretation.summary && <blockquote>{interpretation.summary}</blockquote>}
        <ul>{interpretation.claims.map((claim, index) => <li key={index}>{claim.article_id} [{claim.field}:{claim.start}–{claim.end}]: {claim.quote}</li>)}</ul>
      </>}
      {result && <p>Dropped unsupported claims: {result.dropped.join(', ') || 'None'}</p>}
      <label>Research instrument ID<input value={instrument} disabled={busy} onChange={event => { setInstrument(event.target.value); setAdmission(undefined); }} /></label>
      <button disabled={busy || !instrument.trim() || !view.current_group_matches || view.result.status !== 'GROUNDED_UNVERIFIED'} onClick={admit}>Evaluate quotation admission</button>
      <p>Admission requires independent source-policy support and a mapped instrument inside the quotation. It is not external fact checking or order approval. Model sentiment remains unavailable.</p>
      {admission && <p role="status">{admission.status}; evidence {admission.source_id}; audit {admission.audit_id}; known at {admission.known_at}.</p>}
    </>}
  </section>;
}
