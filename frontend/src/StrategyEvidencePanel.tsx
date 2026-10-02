import { useEffect, useState } from 'react';
import { Api, type Workspace } from './api';
import type { components } from './api-schema';

type Review = components['schemas']['StrategyEvidenceReview'];

export function StrategyEvidencePanel({ api, strategies }: { api: Api; strategies: Workspace['strategies'] }) {
  const [selected, setSelected] = useState('');
  const [data, setData] = useState<Review>();
  const [policy, setPolicy] = useState('');
  const [reason, setReason] = useState('');
  const [backtest, setBacktest] = useState('');
  const [walkforward, setWalkforward] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const strategy = strategies.find(row => JSON.stringify([row.strategy_id, row.version]) === selected);
  const path = strategy ? `/strategies/${encodeURIComponent(strategy.strategy_id)}/evidence` : '';
  const version = strategy?.version;
  useEffect(() => {
    let active = true, pending = false;
    setData(undefined); setPolicy(''); setError(''); setMessage('');
    async function load() {
      if (!path || !version || pending) return;
      pending = true;
      try {
        const result = await api.request<Review>(`${path}?version=${encodeURIComponent(version)}`);
        if (active) { setData(result); setError(''); }
      } catch (failure) { if (active) setError(String(failure)); }
      finally { pending = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, path, version]);
  async function submit(kind: 'policy' | 'review') {
    if (!strategy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const body = kind === 'policy' ? { version, reason, policy: JSON.parse(policy) } : {
        version, reason, confirmation: 'REVIEW STRATEGY EVIDENCE',
        backtest_id: backtest || null, walkforward_id: walkforward || null,
      };
      const result = await api.request<{ audit_event_id: string }>(`${path}/${kind}`, { method: 'POST', body: JSON.stringify(body) });
      setData(await api.request<Review>(`${path}?version=${encodeURIComponent(strategy.version)}`));
      setMessage(`Audited ${kind}: ${result.audit_event_id}. LIVE remains disabled.`);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Strategy evidence"><h2>PAPER evidence eligibility</h2>
    <p>Observed coverage and audited net trades, not inferred trading days. LIVE approval and external verification remain unavailable.</p>
    <label htmlFor="evidence-strategy">Evidence strategy</label><select id="evidence-strategy" value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}>
      <option value="">Select strategy version</option>
      {strategies.map(row => <option key={JSON.stringify([row.strategy_id, row.version])} value={JSON.stringify([row.strategy_id, row.version])}>{row.strategy_id} / {row.version}</option>)}
    </select>
    {!strategies.length && <p>No registered strategies.</p>}
    {path && !data && !error && <p role="status">Loading evidence…</p>}
    {error && <p role="alert">Evidence unavailable or stale: {error}</p>}
    {data && <>
      <p>Eligible sessions: {data.paper.eligible_sessions}; eligible trades: {data.paper.eligible_trades}. Thresholds: {data.paper.threshold_passed ? 'PASSED — NOT LIVE APPROVAL' : 'BLOCKED'}</p>
      <p>Gross P&amp;L: {data.paper.gross_pnl ?? 'UNAVAILABLE'}; estimated charges: {data.paper.charges ?? 'UNAVAILABLE'}; net P&amp;L: {data.paper.net_pnl ?? 'UNAVAILABLE'}</p>
      <p>Expectancy: {data.paper.expectancy ?? 'UNAVAILABLE'}; win fraction: {data.paper.win_rate ?? 'UNAVAILABLE'}</p>
      <p>Blockers: {data.blockers.join(', ')}</p>
      <p>Report linkage: {JSON.stringify(data.report_states)}</p>
      <details><summary>Coverage, exclusions and current policy</summary><pre>{JSON.stringify(data.paper, null, 2)}</pre></details>
    </>}
    <label>Strategy evidence reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <details><summary>Declare versioned PAPER evidence policy</summary>
      <p>Explicit JSON required: version, minimum_sessions, minimum_trades, minimum_session_coverage_fraction, sample_interval_seconds, maximum_sample_gap_seconds. Use the next policy version. A new policy starts new evidence; no prior history is grandfathered.</p>
      <label>PAPER evidence policy JSON<textarea value={policy} onChange={event => setPolicy(event.target.value)} rows={8} /></label>
      <button disabled={!path || busy || !policy || reason.trim().length < 10} onClick={() => void submit('policy')}>Publish evidence policy</button>
    </details>
    <label>Backtest evidence run ID<input value={backtest} onChange={event => setBacktest(event.target.value)} /></label>
    <label>Walk-forward evidence run ID<input value={walkforward} onChange={event => setWalkforward(event.target.value)} /></label>
    <button disabled={!path || busy || reason.trim().length < 10} onClick={() => void submit('review')}>Record strategy evidence review</button>
    {message && <p role="status">{message}</p>}
  </section>;
}
