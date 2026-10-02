import { useCallback, useEffect, useRef, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Page = components['schemas']['DiscrepancyPage'];
type Item = components['schemas']['DiscrepancyView'];

export function ReconciliationPanel({ api }: { api: Api }) {
  const [data, setData] = useState<Page>();
  const [selected, setSelected] = useState<Item>();
  const [resolved, setResolved] = useState(false);
  const [offset, setOffset] = useState(0);
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [outcome, setOutcome] = useState('');
  const [busy, setBusy] = useState(false);
  const activeRead = useRef<AbortController>();
  const load = useCallback(async () => {
    activeRead.current?.abort();
    const controller = new AbortController();
    activeRead.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    setData(undefined); setError('');
    try {
      const result = await api.request<Page>(`/reconciliation?resolved=${resolved}&offset=${offset}`, { signal: controller.signal });
      if (!Array.isArray(result.items)) throw new Error('Invalid discrepancy response');
      if (activeRead.current === controller && !controller.signal.aborted) setData(result);
    } catch (failure) {
      if (activeRead.current === controller) setError(controller.signal.aborted ? 'Discrepancy read timed out; refresh to retry.' : String(failure));
    } finally { window.clearTimeout(timeout); }
  }, [api, resolved, offset]);
  useEffect(() => {
    void load();
    return () => { activeRead.current?.abort(); activeRead.current = undefined; };
  }, [load]);
  async function resolve() {
    if (!selected) return;
    setBusy(true); setOutcome(''); setError('');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 25000);
    try {
      const result = await api.request<Item>(`/reconciliation/${encodeURIComponent(selected.record.id)}/resolve`, {
        method: 'POST', signal: controller.signal, body: JSON.stringify({ expected_head: selected.head_hash, reason, confirmation }),
      });
      if (!result.record.resolved) throw new Error('Resolution not confirmed');
      setSelected(undefined); setConfirmation('');
      await load(); setOutcome('Resolution recorded. Other risk, health and emergency gates remain.');
    } catch (failure) { setError(controller.signal.aborted ? 'Resolution response timed out; refresh current state before retrying.' : String(failure)); }
    finally { window.clearTimeout(timeout); setBusy(false); }
  }
  return <section aria-label="PAPER reconciliation review"><h2>PAPER reconciliation review</h2>
    <p>Resolution requires a running recovered PAPER worker and a fresh matching broker/local comparison. It does not change quantities, fills, cash or P&amp;L, and does not arm trading.</p>
    <label><input type="checkbox" disabled={busy} checked={resolved} onChange={event => { setResolved(event.target.checked); setOffset(0); setSelected(undefined); }} />Show resolved discrepancies</label>
    <button disabled={busy} onClick={() => { setSelected(undefined); void load(); }}>Refresh discrepancies</button>
    {error && <p role="alert">RECONCILIATION UNAVAILABLE: {error}</p>}
    {outcome && <p role="status">{outcome}</p>}
    {!data && !error && <p>Loading discrepancy evidence…</p>}
    {data && <><p>{data.scope}</p>{!data.items.length && <p>No recorded discrepancies in this view; not proof of broker verification.</p>}
      {data.items.map(item => <article key={item.record.id}><h3>{item.record.id}</h3>
        <p>{item.record.detected_at} — {item.record.resolved ? 'REVIEWED' : 'REVIEW REQUIRED'}</p>
        <pre>{JSON.stringify(item.record.delta, null, 2)}</pre>
        {!item.record.resolved && <button onClick={() => { setSelected(item); setConfirmation(''); setOutcome(''); }}>Review {item.record.id}</button>}
      </article>)}
      <button disabled={!offset || busy} onClick={() => { setOffset(Math.max(0, offset - 50)); setSelected(undefined); }}>Previous discrepancies</button>
      <button disabled={!data.has_more || busy} onClick={() => { setOffset(offset + 50); setSelected(undefined); }}>Next discrepancies</button>
    </>}
    {selected && <><h3>Selected evidence: {selected.record.id}</h3><pre>{JSON.stringify(selected.record, null, 2)}</pre>
      <label>Discrepancy review reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <label>Type RESOLVE PAPER DISCREPANCY<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || reason.trim().length < 10 || confirmation !== 'RESOLVE PAPER DISCREPANCY'} onClick={() => void resolve()}>Record authenticated discrepancy resolution</button>
    </>}
  </section>;
}
