import { useEffect, useState, type FormEvent } from 'react';
import { Api, type Workspace } from './api';
import type { components } from './api-schema';

type Candidate = components['schemas']['ReplacementCandidate'];
type Result = components['schemas']['ReplacementResult'];

export function OrderReplacement({ api, orders, mode, onChange }: {
  api: Api; orders: Workspace['orders']; mode: string; onChange: () => void;
}) {
  const [order, setOrder] = useState('');
  const [proposal, setProposal] = useState('');
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Result>();
  const eligible = orders.filter(item => item.mode === 'PAPER' && item.role === 'ENTRY'
    && ['OPEN', 'PENDING'].includes(item.status) && item.filled_quantity === 0);
  function changed() { setRequestId(crypto.randomUUID()); setResult(undefined); setError(''); }
  useEffect(() => {
    let current = true;
    setCandidates([]); setProposal(''); setError('');
    if (!order || mode !== 'PAPER') { setLoading(false); return; }
    setLoading(true);
    void api.request<Candidate[]>(`/orders/${encodeURIComponent(order)}/replacement-proposals`)
      .then(rows => { if (current) setCandidates(rows); })
      .catch(failure => { if (current) setError(String(failure)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [api, order, mode]);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setResult(undefined);
    try {
      setResult(await api.request<Result>(`/orders/${encodeURIComponent(order)}/replace`, {
        method: 'POST', body: JSON.stringify({ request_id: requestId, reason, confirmation,
          replacement_proposal_id: proposal }),
      }));
    } catch (failure) { setError(`${String(failure)}. Outcome unconfirmed; inspect orders and audit. An unchanged retry retains its request ID.`); }
    finally { setBusy(false); onChange(); }
  }
  return <section aria-label="PAPER order replacement"><h2>Replace unfilled PAPER entry</h2>
    <p>Requires a separate approved proposal. Sizing, risk and preflight run again. The original may be cancelled without a replacement being submitted. No direct price or quantity edits.</p>
    {mode !== 'PAPER' ? <p>PAPER controls unavailable in this mode.</p> : <form onSubmit={event => void submit(event)}>
      <label>Original entry<select value={order} disabled={busy} onChange={event => { setOrder(event.target.value); changed(); }}>
        <option value="">Select unfilled entry</option>{eligible.map(item => <option key={item.id} value={item.id}>{item.trading_symbol} — {item.id}</option>)}
      </select></label>
      {loading && <p role="status">Loading approved proposals…</p>}
      {order && !loading && !candidates.length && <p>No replacement proposals available.</p>}
      <label>Replacement approval<select value={proposal} disabled={busy || loading} onChange={event => { setProposal(event.target.value); changed(); }}>
        <option value="">Select proposal</option>{candidates.map(item => <option key={item.proposal_id} value={item.proposal_id}>{item.proposal_id}: entry {item.entry}, stop {item.stop}, target {item.target}, quantity {item.approved_quantity}</option>)}
      </select></label>
      <p>Listed approvals are candidates, not a guarantee of current execution eligibility.</p>
      <label>Replacement reason<input value={reason} disabled={busy} maxLength={500} onChange={event => { setReason(event.target.value); changed(); }} /></label>
      <label>Type REPLACE PAPER ENTRY<input value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || loading || !eligible.some(item => item.id === order) || !candidates.some(item => item.proposal_id === proposal) || reason.length < 10 || confirmation !== 'REPLACE PAPER ENTRY'}>Cancel and revalidate replacement</button>
    </form>}
    {error && <p role="alert">{error}</p>}
    {result && <div role="status"><p>Original: {result.original_status}. Replacement: {result.replacement_status}.</p><p>{result.code}; {result.error ?? 'no recorded error'}</p><p>Audit: {result.audit_chain_id}</p></div>}
  </section>;
}
