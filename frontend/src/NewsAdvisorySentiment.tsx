import { useState, type FormEvent } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

export function NewsAdvisorySentiment({ api }: { api: Api }) {
  const [policy, setPolicy] = useState<components['schemas']['SentimentPolicyView']>();
  const [view, setView] = useState<components['schemas']['AdvisorySentiment']>();
  const [configuration, setConfiguration] = useState('');
  const [reason, setReason] = useState('');
  const [instrument, setInstrument] = useState('');
  const [origin, setOrigin] = useState('LIVE');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function loadPolicy() {
    setBusy(true); setError(''); setPolicy(undefined); setConfiguration(''); setView(undefined);
    try {
      const result = await api.request<components['schemas']['SentimentPolicyView']>('/news/sentiment-policy');
      setPolicy(result); setConfiguration(result.policy ? JSON.stringify(result.policy, null, 2) : '');
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!policy) return;
    setBusy(true); setError(''); setView(undefined);
    try {
      setPolicy(await api.request<components['schemas']['SentimentPolicyView']>('/news/sentiment-policy', {
        method: 'PUT', body: JSON.stringify({ policy: JSON.parse(configuration), expected_event_id: policy.event_id, reason }),
      }));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function resolve(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setView(undefined);
    try {
      setView(await api.request<components['schemas']['AdvisorySentiment']>(`/news/sentiment/${encodeURIComponent(instrument)}?origin=${encodeURIComponent(origin)}`));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section><h3>Advisory news sentiment</h3>
    <p>Model confidence is uncalibrated. Source-policy admission is not fact verification. Sentiment cannot be a standalone trade trigger.</p>
    {busy && <p role="status">Loading advisory sentiment state…</p>}
    {error && <p role="alert">SENTIMENT UNAVAILABLE — {error}</p>}
    <button disabled={busy} onClick={() => void loadPolicy()}>Load sentiment policy</button>
    <p>Policy: {policy ? (policy.policy?.enabled ? 'ENABLED — ADVISORY ONLY' : 'UNAVAILABLE OR DISABLED') : 'NOT LOADED'}; version: {policy?.event_id ?? 'UNAVAILABLE'}</p>
    <form onSubmit={save}>
      <label>Advisory sentiment policy JSON<textarea required disabled={busy || !policy} value={configuration} onChange={event => setConfiguration(event.target.value)} /></label>
      <p>Required: enabled, accept_uncalibrated_model_scores, aggregation with tier_weights (four values), half_life_seconds, max_age_seconds and minimum_confidence. Values are owner-supplied; none are invented.</p>
      <label>Sentiment policy reason<input required minLength={10} maxLength={500} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button disabled={busy || !policy || !configuration}>Save sentiment policy</button>
    </form>
    <form onSubmit={resolve}>
      <label>Sentiment instrument ID<input required disabled={busy} value={instrument} onChange={event => { setInstrument(event.target.value); setView(undefined); }} /></label>
      <label htmlFor="sentiment-data-origin">Sentiment data origin</label><select id="sentiment-data-origin" disabled={busy} value={origin} onChange={event => { setOrigin(event.target.value); setView(undefined); }}><option>LIVE</option><option>REPLAY</option><option>SYNTHETIC</option></select>
      <button disabled={busy || !instrument}>Resolve advisory sentiment</button>
    </form>
    {view && <><p>{view.code}</p><p>Score: {view.status === 'AVAILABLE' ? view.result?.score ?? 'UNAVAILABLE' : 'UNAVAILABLE'}. As of: {view.as_of}. Origin: {view.data_origin}.</p><details><summary>Sentiment evidence and policy</summary><pre>{JSON.stringify(view, null, 2)}</pre></details></>}
  </section>;
}
