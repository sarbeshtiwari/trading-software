import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Page = components['schemas']['RiskDecisionPage'];
type Inspection = components['schemas']['RiskDecisionInspection'];

export function RiskDecisionPanel({ api }: { api: Api }) {
  const [page, setPage] = useState<Page>();
  const [detail, setDetail] = useState<Inspection>();
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    setPage(undefined); setDetail(undefined); setError('');
    void api.request<Page>(`/risk/decisions?offset=${offset}`).then(result => {
      if (!Array.isArray(result.items)) throw new Error('Invalid decision index');
      if (!stopped) setPage(result);
    }).catch(failure => { if (!stopped) setError(String(failure)); });
    return () => { stopped = true; };
  }, [api, offset, revision]);
  async function inspect(identifier: string) {
    setBusy(true); setDetail(undefined); setError('');
    try {
      const result = await api.request<Inspection>(`/risk/decisions/${encodeURIComponent(identifier)}`);
      if (result.id !== identifier || result.historical_only !== true) throw new Error('Invalid historical risk response');
      setDetail(result);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  const decision = detail?.status === 'AUDIT_BOUND_REPLAY_VERIFIED' ? detail.decision : undefined;
  const perTrade = decision?.rules.find(rule => rule.rule === 'per_trade');
  return <section aria-label="Decision risk evidence"><h2>Decision risk evidence</h2>
    <p>Historical evaluation only — never current order authorization. Index entries are unverified until inspected. All amounts and rule results come from the backend.</p>
    {error && <p role="alert">RISK DECISION UNAVAILABLE: {error}</p>}
    {!page && !error && <p>Loading risk decisions…</p>}
    {page && <>
      {!page.items.length && <p>No recorded risk decisions.</p>}
      {page.items.map(row => <p key={row.id}><button disabled={busy} onClick={() => void inspect(row.id)}>Inspect risk {row.id}</button> {row.is_preflight ? 'Preflight' : 'Initial evaluation'} / {row.evaluated_at} / proposal {row.proposal_id}</p>)}
      <button disabled={busy || offset === 0} onClick={() => setOffset(offset - 50)}>Previous risk decisions</button>
      <button disabled={busy || !page.has_more} onClick={() => setOffset(offset + 50)}>Next risk decisions</button>
      <button disabled={busy} onClick={() => setRevision(revision + 1)}>Refresh risk decisions</button>
    </>}
    {detail && <article aria-label="Inspected risk decision">
      <h3>{detail.status}</h3><p>{detail.mode} / {detail.origin ?? 'UNAVAILABLE'} / configuration {detail.configuration_version ?? 'UNAVAILABLE'}</p>
      <p>Decision {detail.id}; proposal {detail.proposal_id}; evaluated {detail.evaluated_at}</p>
      {decision ? <>
        <p>{decision.approved ? 'HISTORICAL APPROVAL — NOT EXECUTION PERMISSION' : `HISTORICAL REJECTION — ${decision.rejection_code}`}</p>
        <p>Binding rule: {decision.binding_rule ?? 'NONE'}; formula {decision.formula_version}</p>
        {perTrade && <p>Per-trade risk / limit: {String(perTrade.inputs.risk)} / {String(perTrade.inputs.limit)}</p>}
        <div className="table-wrap"><table><thead><tr><th>Rule</th><th>Result</th><th>Rejection code</th><th>Recorded inputs and limits</th></tr></thead>
          <tbody>{decision.rules.map(rule => <tr key={rule.rule}><td>{rule.rule}</td><td>{rule.passed ? 'PASS' : 'FAIL'}</td><td>{rule.rejection_code}</td><td>{JSON.stringify(rule.inputs)}</td></tr>)}</tbody>
        </table></div>
      </> : <p role="alert">Decision evidence withheld. No verified risk conclusion is available.</p>}
    </article>}
  </section>;
}
