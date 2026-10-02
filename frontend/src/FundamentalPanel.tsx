import { useEffect, useState, type FormEvent } from 'react';
import { Api } from './api';
import { FundamentalSources } from './FundamentalSources';
import type { components } from './api-schema';

type Detail = components['schemas']['FundamentalDetail'];

export function FundamentalPanel({ api, instrument }: { api: Api; instrument: string }) {
  const [source, setSource] = useState('');
  const [selection, setSelection] = useState('');
  const [data, setData] = useState<Detail>();
  const [error, setError] = useState('');
  useEffect(() => { setSelection(''); setSource(''); setData(undefined); setError(''); }, [instrument]);
  useEffect(() => {
    setData(undefined); setError('');
    if (!selection) return;
    let stopped = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    void api.request<Detail>(`/fundamentals/${encodeURIComponent(instrument)}?source=${encodeURIComponent(selection)}`, { signal: controller.signal }).then(result => {
      if (result.instrument_id !== instrument || result.source !== selection) throw new Error('Fundamental evidence identity mismatch');
      if (!stopped) setData(result);
    }).catch(failure => { if (!stopped) setError(String(failure)); })
      .finally(() => window.clearTimeout(timeout));
    return () => { stopped = true; controller.abort(); window.clearTimeout(timeout); };
  }, [api, instrument, selection]);
  function submit(event: FormEvent) { event.preventDefault(); setSelection(source.trim()); }
  return <section aria-label="Instrument fundamentals"><h3>Instrument fundamentals</h3>
    <FundamentalSources key={instrument} api={api} instrument={instrument} onSelect={value => { setSource(value); setSelection(value); }} />
    <p>Manual source evidence only. Enter the source identifier used by your imported CSV/JSON. No Groww fundamentals feed is assumed.</p>
    <form onSubmit={submit}><label>Fundamentals source<input value={source} onChange={event => setSource(event.target.value)} required /></label><button disabled={!source.trim()}>Inspect fundamentals</button></form>
    {selection && !data && !error && <p>Loading fundamental evidence…</p>}
    {error && <p role="alert">FUNDAMENTALS UNAVAILABLE: {error}</p>}
    {data && <><p>Fundamental record: {data.status}; source: {data.source}; as of {data.as_of}</p>
      <p>Freshness limit: {data.max_age_days} days. Record availability does not imply all metrics are usable.</p>
      {data.view.evidence && <p>Record {data.view.record_id}; period ending {data.view.evidence.period_end}; known at {data.view.evidence.known_at}</p>}
      {Object.values(data.view.exclusions).includes('STALE') && <p role="alert">FUNDAMENTALS STALE — excluded metrics are not used in calculated scores.</p>}
      <div className="table-wrap"><table><thead><tr><th>Metric</th><th>Usable value</th><th>Observation time</th><th>Exclusion</th></tr></thead>
        <tbody>{Object.entries(data.view.metrics).map(([name, metric]) => <tr key={name}><td>{name}</td><td>{metric?.value ?? 'UNAVAILABLE'}</td><td>{metric?.as_of ?? 'UNAVAILABLE'}</td><td>{data.view.exclusions[name] ?? 'NONE'}</td></tr>)}</tbody></table></div>
      <details><summary>Backend valuation, quality and scoring evidence</summary><pre>{JSON.stringify({ valuation: data.valuation, quality: data.quality, health: data.health, score: data.score }, null, 2)}</pre></details>
      <p>Scores are deterministic research summaries, not trading permission or profitability evidence.</p>
    </>}
  </section>;
}
