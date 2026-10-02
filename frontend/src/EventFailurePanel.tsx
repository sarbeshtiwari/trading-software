import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type View = components['schemas']['EventFailureView'];

export function EventFailurePanel({ api }: { api: Api }) {
  const [data, setData] = useState<View>();
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    let pending = false;
    let controller: AbortController | undefined;
    async function load() {
      if (pending) return;
      pending = true; controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 8000);
      try {
        const result = await api.request<View>('/events/failures', { signal: controller.signal });
        if (!Array.isArray(result.failures)) throw new Error('Invalid failure report');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) { if (!stopped) { setData(undefined); setError(String(failure)); } }
      finally { pending = false; window.clearTimeout(timeout); }
    }
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer); };
  }, [api]);
  return <section aria-label="Retained event failures"><h2>Retained event failures</h2>
    {!data && !error && <p>Loading event delivery evidence…</p>}
    {error && <p role="alert">EVENT MONITOR UNAVAILABLE: {error}</p>}
    {data && <><p role={data.status === 'NO_RETAINED_FAILURES' ? 'status' : 'alert'}>{data.status}</p>
      <p>{data.scope}</p><p>Audit/outbox publisher: {data.publication_status ?? 'UNAVAILABLE'} — not external delivery confirmation.</p><p>OMS event publisher: {data.runtime_publication_status ?? 'UNAVAILABLE'} — not broker connectivity.</p><p>Retained records: {data.retained_count ?? 'UNAVAILABLE'}</p>
      {data.has_more && <p>Showing latest 50 records; older retained records remain in Redis.</p>}
      {data.failures?.length ? <div className="table-wrap"><table><thead><tr><th>Receipt</th><th>Event type</th><th>Original entry</th><th>Handler reference</th><th>Classification</th></tr></thead>
        <tbody>{data.failures.map(row => <tr key={row.receipt_id}><td>{row.receipt_id}</td><td>{row.event_type}</td><td>{row.entry_id ?? 'UNAVAILABLE'}</td><td>{row.handler_reference}</td><td>{row.category}</td></tr>)}</tbody></table></div> : <p>No retained failure details available. This does not certify runtime event delivery.</p>}
    </>}
  </section>;
}
