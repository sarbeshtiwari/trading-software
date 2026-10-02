import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type View = components['schemas']['HealthHistory'];

export function HealthHistoryPanel({ api }: { api: Api }) {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [query, setQuery] = useState({ params: '' });
  const [data, setData] = useState<View>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let stopped = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    setLoading(true); setData(undefined); setError('');
    api.request<View>(`/health/history${query.params ? `?${query.params}` : ''}`, { signal: controller.signal })
      .then(result => { if (!Array.isArray(result.observations)) throw new Error('Invalid health history'); if (!stopped) setData(result); })
      .catch(failure => { if (!stopped) setError(String(failure)); })
      .finally(() => { if (!stopped) setLoading(false); window.clearTimeout(timeout); });
    return () => { stopped = true; controller.abort(); window.clearTimeout(timeout); };
  }, [api, query]);
  function inspect() {
    const params = new URLSearchParams();
    if (from) params.set('from_at', new Date(`${from}Z`).toISOString());
    if (to) params.set('to_at', new Date(`${to}Z`).toISOString());
    setQuery({ params: params.toString() });
  }
  function next() {
    if (!data || data.next_offset === null) return;
    setQuery({ params: new URLSearchParams({ from_at: data.from_at, to_at: data.to_at, offset: String(data.next_offset) }).toString() });
  }
  return <section aria-label="Health transition history"><h2>Health transition history</h2>
    <p>Audited observations, not continuous health guarantees. UTC interval, at most 31 days; blank fields default to the latest 24 hours.</p>
    <label>Health history from (UTC)<input type="datetime-local" value={from} onChange={event => setFrom(event.target.value)} /></label>
    <label>Health history to (UTC)<input type="datetime-local" value={to} onChange={event => setTo(event.target.value)} /></label>
    <button onClick={inspect} disabled={loading}>Inspect health history</button>
    {loading && <p>Loading health observations…</p>}
    {error && <p role="alert">HEALTH HISTORY UNAVAILABLE: {error}</p>}
    {data && <><p>{data.mode}: {data.from_at} through {data.to_at}</p><p>{data.scope}</p>
      {data.preceding ? <p>Last observation before interval: {data.preceding.observed_at}, audit {data.preceding.audit_id}. This does not establish health during the interval.</p> : <p>No observation before this interval is available.</p>}
      {data.observations.length ? <div className="table-wrap"><table><thead><tr><th>Observed</th><th>Audit</th><th>Components</th><th>Blocked</th><th>Missing</th></tr></thead>
        <tbody>{data.observations.map(row => <tr key={row.audit_id}><td>{row.observed_at}</td><td>{row.audit_id}</td><td>{Object.entries(row.state.checks).map(([name, check]) => `${name}: ${check.status}${check.critical ? ' (critical)' : ''}`).join('; ')}</td><td>{row.state.blocked.join(', ') || 'None recorded'}</td><td>{row.state.missing.join(', ') || 'None recorded'}</td></tr>)}</tbody></table></div> : <p>No transitions recorded in this interval. This is not proof of healthy service.</p>}
      <button onClick={next} disabled={!data.has_more}>Next health observations</button>
    </>}
  </section>;
}
