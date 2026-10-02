import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Sources = components['schemas']['FundamentalSources'];

export function FundamentalSources({ api, instrument, onSelect }: { api: Api; instrument: string; onSelect: (source: string) => void }) {
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Sources>();
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    setData(undefined); setError('');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    void api.request<Sources>(`/fundamentals/${encodeURIComponent(instrument)}/sources?offset=${offset}`, { signal: controller.signal }).then(result => {
      if (result.instrument_id !== instrument || !Array.isArray(result.sources)) throw new Error('Invalid source catalog');
      if (!stopped) setData(result);
    }).catch(failure => { if (!stopped) setError(String(failure)); })
      .finally(() => window.clearTimeout(timeout));
    return () => { stopped = true; controller.abort(); window.clearTimeout(timeout); };
  }, [api, instrument, offset]);
  return <section aria-label="Imported fundamental sources">
    <h4>Imported sources</h4>
    {!data && !error && <p>Loading source receipts…</p>}
    {error && <p role="status">SOURCE CATALOG UNAVAILABLE: {error}. Manual source inspection remains available.</p>}
    {data && <><p>Known and received by {data.as_of}. A receipt is not freshness or external source verification.</p>
      {!data.sources.length && <p>No imported sources available at this cutoff.</p>}
      {data.sources.map(row => <div key={row.source}><button onClick={() => onSelect(row.source)}>Inspect source {row.source}</button><p>Record {row.record_id}; known {row.known_at}; received {row.received_at}</p></div>)}
      <button disabled={offset === 0} onClick={() => setOffset(value => Math.max(0, value - 50))}>Previous sources</button>
      <button disabled={!data.has_more} onClick={() => setOffset(value => value + 50)}>Next sources</button>
    </>}
  </section>;
}
