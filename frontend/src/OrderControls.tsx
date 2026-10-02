import { useState, type FormEvent } from 'react';
import { Api, type Workspace } from './api';
import type { components } from './api-schema';

export function OrderControls({ api, orders, mode, onChange }: {
  api: Api; orders: Workspace['orders']; mode: string; onChange: () => void;
}) {
  const [selected, setSelected] = useState('');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<components['schemas']['CancelEntryResult']>();
  const eligible = orders.filter(order => order.mode === 'PAPER' && order.role === 'ENTRY'
    && !['EXECUTED', 'CANCELLED', 'REJECTED'].includes(order.status));
  function changed() { setRequestId(crypto.randomUUID()); setResult(undefined); setError(''); }
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setResult(undefined);
    try {
      const outcome = await api.request<components['schemas']['CancelEntryResult']>(`/orders/${encodeURIComponent(selected)}/cancel`, {
        method: 'POST', body: JSON.stringify({ request_id: requestId, reason, confirmation }),
      });
      setResult(outcome); onChange();
    } catch (failure) { setError(String(failure)); onChange(); }
    finally { setBusy(false); }
  }
  return <section aria-label="PAPER order cancellation"><h2>Cancel PAPER entry</h2>
    <p>Only the unfilled entry remainder is cancelled. Existing fills remain positions and require protection. Protective exit orders cannot be cancelled here. Modification is not available.</p>
    {mode !== 'PAPER' ? <p>PAPER controls unavailable in this mode.</p> : <form onSubmit={event => void submit(event)}>
      <label>Entry order<select disabled={busy} value={selected} onChange={event => { setSelected(event.target.value); changed(); }}>
        <option value="">Select pending entry</option>{eligible.map(order => <option key={order.id} value={order.id}>{order.trading_symbol} — {order.id} — {order.status}</option>)}
      </select></label>
      {!eligible.length && <p>No cancellable PAPER entry orders recorded.</p>}
      <label>Cancellation reason<input disabled={busy} maxLength={500} value={reason} onChange={event => { setReason(event.target.value); changed(); }} /></label>
      <label>Type CANCEL PAPER ENTRY<input disabled={busy} value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      <button disabled={busy || !eligible.some(order => order.id === selected) || reason.length < 10 || confirmation !== 'CANCEL PAPER ENTRY'}>{busy ? 'Cancellation pending…' : 'Cancel entry remainder'}</button>
    </form>}
    {error && <p role="alert">{error}. Outcome is not confirmed; inspect orders and audit before retrying. An unchanged request reuses its request ID.</p>}
    {result && <div role="status"><p>Recorded outcome: {result.status}; filled quantity: {result.filled_quantity ?? 'UNAVAILABLE'}.</p>
      <p>{result.terminal && !result.error ? 'Terminal order state recorded. Remaining positions still require monitoring.' : 'CANCELLATION UNRESOLVED — inspect reconciliation and monitoring.'}</p>
      <p>Audit: {result.audit_chain_id}{result.error && `; error: ${result.error}`}</p></div>}
  </section>;
}
