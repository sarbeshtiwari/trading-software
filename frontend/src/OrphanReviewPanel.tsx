import { useCallback, useEffect, useRef, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Page = components['schemas']['OrphanReviewPage'];
type Item = components['schemas']['OrphanReview'];
type Plan = components['schemas']['PositionRecoveryPlan'];

export function OrphanReviewPanel({ api }: { api: Api }) {
  const [data, setData] = useState<Page>();
  const [selected, setSelected] = useState<Item>();
  const [plan, setPlan] = useState<Plan>();
  const [offset, setOffset] = useState(0);
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [outcome, setOutcome] = useState('');
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController>();
  const load = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    setData(undefined); setError(''); setPlan(undefined);
    try {
      const result = await api.request<Page>(`/reconciliation/orphans?offset=${offset}`, { signal: controller.signal });
      if (!Array.isArray(result.items)) throw new Error('Invalid orphan response');
      if (active.current === controller && !controller.signal.aborted) setData(result);
    } catch (failure) {
      if (active.current === controller) setError(controller.signal.aborted ? 'Inspection timed out; refresh to retry.' : String(failure));
    } finally { window.clearTimeout(timeout); }
  }, [api, offset]);
  useEffect(() => {
    void load();
    return () => { active.current?.abort(); active.current = undefined; };
  }, [load]);
  async function acknowledge() {
    if (!selected) return;
    setBusy(true); setError(''); setOutcome('');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const result = await api.request<Item>(`/reconciliation/orphans/${encodeURIComponent(selected.position_id)}/acknowledge`, {
        method: 'POST', signal: controller.signal,
        body: JSON.stringify({ reason, confirmation, expected_head: selected.head_hash }),
      });
      if (!result.acknowledged) throw new Error('Acknowledgment not confirmed');
      setSelected(undefined); setConfirmation('');
      await load(); setOutcome('Owner acknowledgment recorded. Entries remain blocked; accounting and protection recovery are still required.');
    } catch (failure) {
      setError(controller.signal.aborted ? 'Response timed out; inspect current state before retrying.' : String(failure));
    } finally { window.clearTimeout(timeout); setBusy(false); }
  }
  async function inspectPlan(item: Item) {
    setBusy(true); setPlan(undefined); setError('');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 18000);
    try {
      const result = await api.request<Plan>(`/reconciliation/orphans/${encodeURIComponent(item.position_id)}/recovery-plan`, { signal: controller.signal });
      setPlan(result);
    } catch (failure) { setError(controller.signal.aborted ? 'Recovery inspection timed out.' : String(failure)); }
    finally { window.clearTimeout(timeout); setBusy(false); }
  }
  return <section aria-label="PAPER orphan review"><h2>PAPER orphan review</h2>
    <p>These are historical broker observations, not fresh market verification. Acknowledgment never repairs P&amp;L, protects/closes a position, or arms trading.</p>
    <button disabled={busy} onClick={() => { setSelected(undefined); void load(); }}>Refresh orphan evidence</button>
    {error && <p role="alert">ORPHAN REVIEW UNAVAILABLE: {error}</p>}
    {outcome && <p role="status">{outcome}</p>}
    {!data && !error && <p>Loading orphan evidence…</p>}
    {data && <>{!data.items.length && <p>No recorded orphans in this view; not proof of broker reconciliation.</p>}
      {data.items.map(item => <article key={item.position_id}><h3>{item.position_id}</h3>
        <p>{item.trading_symbol}: {item.quantity} units; observed average {item.observed_average_price}; observed at {item.observed_at}</p>
        <p>{item.acknowledged ? `ACKNOWLEDGED by ${item.acknowledged_by} at ${item.acknowledged_at}` : 'OWNER ACKNOWLEDGMENT REQUIRED'}</p>
        <p>{item.blockers.join(' / ')}</p>
        {!item.acknowledged && <button disabled={busy} onClick={() => { setSelected(item); setConfirmation(''); setOutcome(''); }}>Review orphan {item.position_id}</button>}
        {item.acknowledged && <button disabled={busy} onClick={() => void inspectPlan(item)}>Inspect recovery plan {item.position_id}</button>}
      </article>)}
      <button disabled={!offset || busy} onClick={() => { setOffset(Math.max(0, offset - 50)); setSelected(undefined); }}>Previous orphans</button>
      <button disabled={!data.has_more || busy} onClick={() => { setOffset(offset + 50); setSelected(undefined); }}>Next orphans</button>
    </>}
    {plan && <section aria-label="Verified historical recovery plan"><h3>Verified historical recovery plan</h3>
      <p>This does not restore the position or arm trading. Recorded charges do not imply complete fees. Fresh broker checks and protection recovery remain required.</p>
      <pre>{JSON.stringify(plan, null, 2)}</pre>
    </section>}
    {selected && <><pre>{JSON.stringify(selected, null, 2)}</pre>
      <label>Orphan acknowledgment reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <label>Type ACKNOWLEDGE PAPER ORPHAN<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || reason.trim().length < 10 || confirmation !== 'ACKNOWLEDGE PAPER ORPHAN'} onClick={() => void acknowledge()}>Record orphan acknowledgment</button>
    </>}
  </section>;
}
