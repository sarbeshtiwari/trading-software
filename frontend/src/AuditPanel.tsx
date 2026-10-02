import { useEffect, useState, type FormEvent } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

export function AuditPanel({ api }: { api: Api }) {
  const [page, setPage] = useState<components['schemas']['AuditPage']>();
  const [trail, setTrail] = useState<components['schemas']['AuditTrail']>();
  const [identifier, setIdentifier] = useState('');
  const [instrument, setInstrument] = useState('');
  const [day, setDay] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function search(event?: FormEvent) {
    event?.preventDefault(); setBusy(true); setError(''); setPage(undefined); setTrail(undefined);
    const query = new URLSearchParams();
    if (instrument) query.set('instrument_id', instrument);
    if (day) query.set('day', day);
    try { setPage(await api.request(`/audit/events?${query}`)); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  useEffect(() => { void search(); }, [api]);
  async function inspect(value: string) {
    setBusy(true); setTrail(undefined); setError('');
    try { setTrail(await api.request(`/audit/trails/${encodeURIComponent(value)}`)); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section><h2>Stored audit evidence</h2>
    <p>Search metadata is unverified until inspected. Snapshots are historical, not live market state. Chain consistency does not prove completeness or external truth.</p>
    <form onSubmit={search}><label>Instrument ID<input value={instrument} onChange={event => setInstrument(event.target.value)} /></label>
      <label>Session day (IST)<input type="date" value={day} onChange={event => setDay(event.target.value)} /></label>
      <button disabled={busy}>Search audit</button></form>
    <form onSubmit={event => { event.preventDefault(); void inspect(identifier); }}><label>Decision, proposal, trade or audit ID<input maxLength={64} value={identifier} onChange={event => setIdentifier(event.target.value)} /></label>
      <button disabled={busy || !identifier.trim()}>Inspect ID</button></form>
    {busy && <p role="status">Loading audit evidence...</p>}
    {error && <p role="alert">AUDIT UNAVAILABLE: {error}</p>}
    {page && <><p>{page.events.length ? 'Recorded events' : 'No recorded events match.'}{page.has_more && ' First 50 results; narrow the filters.'}</p>
      {page.events.map(event => <div key={event.id}><button disabled={busy} onClick={() => void inspect(event.id)}>Inspect {event.event_type}</button> {event.occurred_at} {event.decision} {event.id}</div>)}</>}
    {trail && <article><h3>Trail {trail.requested_id}</h3><p>{trail.scope}</p>
      {trail.gaps.map(gap => <p role="alert" key={gap}>{gap}</p>)}
      {trail.chains.map(chain => <section key={chain.id}><h4>{chain.id}: {chain.integrity}</h4>
        {chain.records.map(record => <details key={record.id}><summary>{record.event_type} {record.decision} — {record.occurred_at}</summary><pre>{JSON.stringify(record.snapshot, null, 2)}</pre></details>)}</section>)}
      {trail.journals.map(journal => <details key={journal.id}><summary>Journal {journal.id}: {journal.integrity}</summary><pre>{journal.snapshot ? JSON.stringify(journal.snapshot, null, 2) : 'Evidence withheld'}</pre></details>)}
    </article>}
  </section>;
}
