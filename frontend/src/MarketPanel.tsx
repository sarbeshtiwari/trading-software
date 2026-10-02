import { useEffect, useState, type FormEvent } from 'react';
import { Api } from './api';
import { CandleChart } from './CandleChart';
import { FundamentalPanel } from './FundamentalPanel';
import type { components } from './api-schema';

type Instruments = components['schemas']['InstrumentPage'];
type Chart = components['schemas']['CandleChart'];

export function MarketPanel({ api }: { api: Api }) {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [catalog, setCatalog] = useState<Instruments>();
  const [instrument, setInstrument] = useState('');
  const [origin, setOrigin] = useState('LIVE');
  const [interval, setIntervalMinutes] = useState('1');
  const [data, setData] = useState<Chart>();
  const [error, setError] = useState('');
  const [catalogError, setCatalogError] = useState('');
  useEffect(() => {
    let stopped = false;
    setCatalog(undefined); setCatalogError(''); setInstrument('');
    void api.request<Instruments>(`/market/instruments?query=${encodeURIComponent(search)}`).then(result => {
      if (!Array.isArray(result.instruments)) throw new Error('Invalid instrument response');
      if (!stopped) setCatalog(result);
    }).catch(failure => { if (!stopped) setCatalogError(String(failure)); });
    return () => { stopped = true; };
  }, [api, search]);
  useEffect(() => {
    let stopped = false;
    let pending = false;
    let controller: AbortController | undefined;
    setData(undefined); setError('');
    if (!instrument) return;
    async function load() {
      if (pending) return;
      pending = true; controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 8000);
      try {
        const result = await api.request<Chart>(`/market/candles/${encodeURIComponent(instrument)}?origin=${origin}&interval=${interval}`, { signal: controller.signal });
        if (result.instrument_id !== instrument || result.origin !== origin || result.interval_minutes !== Number(interval) || !Array.isArray(result.bars)) throw new Error('Invalid candle response');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) { if (!stopped) { setData(undefined); setError(String(failure)); } }
      finally { pending = false; window.clearTimeout(timeout); }
    }
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer); };
  }, [api, instrument, origin, interval]);
  function submit(event: FormEvent) { event.preventDefault(); setSearch(query.trim()); }
  return <section aria-label="Stored market observations"><h2>Stored market observations</h2>
    <p>Catalog membership is not trading permission. These are stored closed candles, not a live LTP feed.</p>
    <form onSubmit={submit}><label>Symbol search<input value={query} onChange={event => setQuery(event.target.value)} /></label><button>Search instruments</button></form>
    {catalogError && <p role="alert">CATALOG UNAVAILABLE: {catalogError}</p>}
    {!catalog && !catalogError && <p>Loading instrument catalog…</p>}
    {catalog && <><label>Chart instrument<select aria-label="Chart instrument" value={instrument} onChange={event => setInstrument(event.target.value)}><option value="">Choose instrument</option>
      {catalog.instruments.map(row => <option key={row.id} value={row.id}>{row.exchange}/{row.segment}/{row.symbol}{row.restricted ? ' — RESTRICTED' : ''}{!row.active ? ' — INACTIVE' : ''}</option>)}</select></label>
      {!catalog.instruments.length && <p>No matching instruments.</p>}{catalog.has_more && <p>First 50 matches; refine the search.</p>}</>}
    <label>Chart data origin<select aria-label="Chart data origin" value={origin} onChange={event => setOrigin(event.target.value)}>{['LIVE', 'HISTORICAL', 'REPLAY', 'SYNTHETIC'].map(value => <option key={value}>{value}</option>)}</select></label>
    <label>Chart interval<select aria-label="Chart interval" value={interval} onChange={event => setIntervalMinutes(event.target.value)}>{[1, 5, 10, 60, 240].map(value => <option key={value} value={value}>{value} minutes</option>)}</select></label>
    {error && <p role="alert">MARKET DATA UNAVAILABLE: {error}</p>}
    {instrument && !data && !error && <p>Loading closed candles…</p>}
    {data && <><p>{data.origin}: {data.status}</p><p>As of {data.as_of}; {data.scope}</p>
      {data.status === 'STALE' && <p role="alert">MARKET DATA STALE — chart is historical context only.</p>}
      <p>Time discontinuities, including session breaks: {data.discontinuities}</p>
      {data.bars?.length ? <><CandleChart data={data} /><details><summary>Stored candle values</summary><div className="table-wrap"><table><thead><tr><th>Timestamp</th><th>Close</th><th>Volume</th><th>Backend SMA20</th><th>Ingested</th></tr></thead>
        <tbody>{data.bars.map(bar => <tr key={bar.ts}><td>{bar.ts}</td><td>{bar.close}</td><td>{bar.volume}</td><td>{bar.sma20 ?? 'UNAVAILABLE'}</td><td>{bar.ingested_at}</td></tr>)}</tbody></table></div></details></> : <p>No usable closed candles for this selection. No prices have been fabricated.</p>}
    </>}
    {instrument && <FundamentalPanel key={instrument} api={api} instrument={instrument} />}
  </section>;
}
