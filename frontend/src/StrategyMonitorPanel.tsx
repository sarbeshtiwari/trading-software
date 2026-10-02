import { useEffect, useState } from 'react';
import { Api, type Workspace } from './api';
import type { components } from './api-schema';

type Review = components['schemas']['DegradationView'];

export function StrategyMonitorPanel({ api, strategies }: { api: Api; strategies: Workspace['strategies'] }) {
  const [selected, setSelected] = useState('');
  const [data, setData] = useState<Review>();
  const [policy, setPolicy] = useState('');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const strategy = strategies.find(row => JSON.stringify([row.strategy_id, row.version]) === selected);
  const path = strategy ? `/strategies/${encodeURIComponent(strategy.strategy_id)}/degradation` : '';
  const version = strategy?.version;
  useEffect(() => {
    let active = true, pending = false;
    setData(undefined); setError(''); setMessage(''); setConfirmation('');
    async function load() {
      if (!path || !version || pending) return;
      pending = true;
      try {
        const result = await api.request<Review>(`${path}?version=${encodeURIComponent(version)}`);
        if (active) { setData(result); setError(''); }
      } catch (failure) { if (active) { setData(undefined); setError(String(failure)); } }
      finally { pending = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, path, version]);
  async function submit(kind: 'policy' | 'reset') {
    if (!strategy || !path) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const body = kind === 'policy' ? { version, reason, policy: JSON.parse(policy) } : {
        version, reason, confirmation, policy_event_id: data?.policy_event_id, last_event_id: data?.last_event_id,
      };
      const result = await api.request<{ audit_event_id: string }>(`${path}/${kind}`, { method: 'POST', body: JSON.stringify(body) });
      setData(await api.request<Review>(`${path}?version=${encodeURIComponent(strategy.version)}`));
      setMessage(`Audited ${kind}: ${result.audit_event_id}. Reset does not enable trading.`);
      setConfirmation('');
    } catch (failure) { setError(String(failure)); setData(undefined); }
    finally { setBusy(false); }
  }
  return <section aria-label="Strategy degradation"><h2>PAPER strategy degradation</h2>
    <p>Audited closed-trade net drawdown in currency, not portfolio mark-to-market. Missing costs are unavailable, not zero. No live approval or profitability claim.</p>
    <label htmlFor="monitor-strategy">Monitored strategy</label><select id="monitor-strategy" value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}>
      <option value="">Select strategy version</option>
      {strategies.map(row => <option key={JSON.stringify([row.strategy_id, row.version])} value={JSON.stringify([row.strategy_id, row.version])}>{row.strategy_id} / {row.version}</option>)}
    </select>
    {!strategies.length && <p>No registered strategies to monitor.</p>}
    {path && !data && !error && <p role="status">Loading degradation state…</p>}
    {error && <p role="alert">Degradation state unavailable or stale: {error}</p>}
    {data && <>
      <p>{data.auto_disabled ? `STRATEGY AUTO-DISABLED — ${data.disable_reason}` : `Monitor: ${data.status}`}</p>
      <p>Evidence state: {data.status}; window trades: {data.journal_ids.length}</p>
      <p>Net P&amp;L: {data.net_pnl ?? 'UNAVAILABLE'}; maximum closed-trade drawdown: {data.maximum_drawdown_amount ?? 'UNAVAILABLE'}</p>
      <p>Expectancy: {data.expectancy ?? 'UNAVAILABLE'}; win fraction: {data.win_rate ?? 'UNAVAILABLE'}</p>
      <p>Blockers: {data.blockers.join(', ') || 'None recorded'}</p>
      <details><summary>Monitor policy and journal lineage</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>
    </>}
    <label>Degradation control reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <details><summary>Declare versioned degradation policy</summary>
      <p>Explicit JSON: version, window_trades, minimum_trades, maximum_drawdown_amount, data_origin. No capital or threshold is assumed. Existing history remains in the window after policy changes.</p>
      <label>Degradation policy JSON<textarea rows={6} value={policy} onChange={event => setPolicy(event.target.value)} /></label>
      <button disabled={!path || busy || !policy || reason.trim().length < 10} onClick={() => void submit('policy')}>Publish degradation policy</button>
    </details>
    <label>Type RESET STRATEGY DEGRADATION<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
    <button disabled={busy || !!error || !data?.auto_disabled || data.status !== 'HEALTHY' || reason.trim().length < 10 || confirmation !== 'RESET STRATEGY DEGRADATION'} onClick={() => void submit('reset')}>Reset reviewed degradation latch</button>
    {message && <p role="status">{message}</p>}
  </section>;
}
