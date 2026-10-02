import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Reports = components['schemas']['PaperSummaryList'];
const shown = (value: unknown) => value === null || value === undefined ? 'UNAVAILABLE' : String(value);

export function PaperSummaryPanel({ api }: { api: Api }) {
  const [data, setData] = useState<Reports>();
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let stopped = false;
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await api.request<Reports>('/reports/paper/daily');
        if (!Array.isArray(result.reports)) throw new Error('Invalid summary response');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) {
        if (!stopped) { setData(undefined); setError(String(failure)); }
      } finally { pending = false; }
    }
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [api, revision]);
  return <section><h2>PAPER session observations</h2>
    <p>Immutable observations, not proof of full-session coverage or external delivery. Recovery appends a new record; estimates are not broker bills.</p>
    <button onClick={() => setRevision(value => value + 1)}>Refresh session summaries</button>
    {error && <p role="alert" className="error">Session summaries unavailable or stale: {error}</p>}
    {!error && !data && <p>Loading session summaries…</p>}
    {data && !data.reports.length && <p>No recorded PAPER session summaries.</p>}
    {data?.reports.map(report => <article key={report.audit_id}>
      <h3>{report.summary.session_date} — {report.summary.scope}</h3>
      <p>PAPER / SIMULATED — observed at {report.summary.as_of}</p>
      <p>Reconciliation: {report.summary.reconciliation}; owner review: {report.summary.worker_review_required === null ? 'UNAVAILABLE' : report.summary.worker_review_required ? 'REQUIRED' : 'not flagged in this observation'}</p>
      <details><summary>Order hygiene and protection evidence</summary>
        <p>Unresolved order IDs: {report.summary.order_history_available === false ? 'UNAVAILABLE' : report.summary.unresolved_order_ids.join(', ') || 'None recorded'}</p>
        {report.summary.order_hygiene ? <><p>{report.summary.order_hygiene.scope}</p>
          <p>Recorded protection failure IDs: {report.summary.order_hygiene.protection_failure_ids.join(', ') || 'None recorded; not proof of protection'}</p>
          {report.summary.order_hygiene.cancellations.map(action => <p key={action.chain_id}>{action.order_id}: {action.reason} / {action.status}; filled quantity {action.filled_quantity ?? 'UNAVAILABLE'}; error {action.error ?? 'none recorded'}; audit {action.audit_id}</p>)}
        </> : <p>Order hygiene history UNAVAILABLE.</p>}
      </details>
      {report.summary.reconstruction_status && <p>Historical evidence: {report.summary.reconstruction_status}. No full-session coverage is inferred.</p>}
      <dl><dt>Fills</dt><dd>{shown(report.summary.fill_count)}</dd>
        <dt>Gross realised P&amp;L</dt><dd>{shown(report.summary.gross_realised_pnl)}</dd>
        <dt>Charges ({report.summary.cost_status})</dt><dd>{shown(report.summary.charges)}</dd>
        <dt>Net realised P&amp;L</dt><dd>{shown(report.summary.net_realised_pnl)}</dd>
        <dt>Open positions</dt><dd>{report.summary.position_history_available === false ? 'UNAVAILABLE' : report.summary.open_positions.length}</dd>
        <dt>Unresolved orders</dt><dd>{report.summary.order_history_available === false ? 'UNAVAILABLE' : report.summary.unresolved_order_ids.length}</dd>
        <dt>Critical audit events</dt><dd>{report.summary.error_history_available === false ? 'UNAVAILABLE' : report.summary.critical_event_ids.length}</dd>
        <dt>Valuation</dt><dd>{report.summary.valuation?.status ?? 'UNAVAILABLE'} — {report.summary.valuation?.reason ?? report.summary.limit_utilisation.unavailable_reason ?? 'PAPER estimate'}</dd></dl>
      {(['daily_loss', 'drawdown', 'exposure'] as const).map(name => {
        const value = report.summary.limit_utilisation[name];
        return <p key={name}>{name.replaceAll('_', ' ')}: {value ? `${value.amount} / ${value.limit}; utilisation ${shown(value.utilisation_pct)}%; ${value.at_or_above_limit ? 'AT OR ABOVE LIMIT' : 'below limit in this observation'}` : 'UNAVAILABLE'}</p>;
      })}
      <p>Audit: {report.audit_id}; chain {report.chain_id}, sequence {report.sequence}</p>
      {report.summary.previous_summary_id && <p>Previous observation: {report.summary.previous_summary_id}</p>}
    </article>)}
  </section>;
}
