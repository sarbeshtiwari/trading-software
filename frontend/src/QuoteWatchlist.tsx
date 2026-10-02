import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Quote = components['schemas']['StoredQuoteView'];

function QuoteRow({ api, identifier, origin, remove }: { api: Api; identifier: string; origin: string; remove: () => void }) {
  const [data, setData] = useState<Quote>();
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    let pending = false;
    let controller: AbortController | undefined;
    setData(undefined); setError('');
    async function load() {
      if (pending) return;
      pending = true;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 8000);
      try {
        const result = await api.request<Quote>(`/market/quotes/${encodeURIComponent(identifier)}?origin=${origin}`, { signal: controller.signal });
        if (result.instrument_id !== identifier || result.origin !== origin) throw new Error('Quote identity mismatch');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) { if (!stopped) { setData(undefined); setError(String(failure)); } }
      finally { pending = false; window.clearTimeout(timeout); }
    }
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer); };
  }, [api, identifier, origin]);
  const usable = data?.status === 'RECORDED';
  return <tr><td>{identifier}</td><td>{origin}</td><td>{error ? `UNAVAILABLE: ${error}` : data?.status ?? 'LOADING'}</td>
    <td>{usable ? data?.ltp ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td><td>{usable ? data?.change_pct ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td><td>{usable ? data?.volume ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td>
    <td>{data?.observed_at ?? 'UNAVAILABLE'}</td><td><button onClick={remove}>Remove {identifier}</button></td></tr>;
}

export function QuoteWatchlist({ api, instrument, origin }: { api: Api; instrument: string; origin: string }) {
  const [identifiers, setIdentifiers] = useState<string[]>([]);
  return <section aria-label="Stored quote watchlist"><h3>Stored quote watchlist</h3>
    <p>Audited PAPER ingestion observations only, not verified broker connectivity. Refreshes every three seconds; stale prices are withheld. Selections last for this view session.</p>
    <button disabled={!instrument || identifiers.includes(instrument) || identifiers.length >= 10} onClick={() => setIdentifiers(previous => [...previous, instrument])}>Watch selected instrument</button>
    {!identifiers.length && <p>No watched instruments. Select a catalog instrument to add it.</p>}
    {identifiers.length > 0 && <div className="table-wrap"><table><thead><tr><th>Instrument</th><th>Origin</th><th>Status</th><th>Stored LTP</th><th>Change %</th><th>Volume</th><th>Observed</th><th>Action</th></tr></thead>
      <tbody>{identifiers.map(identifier => <QuoteRow key={`${identifier}:${origin}`} api={api} identifier={identifier} origin={origin} remove={() => setIdentifiers(previous => previous.filter(value => value !== identifier))} />)}</tbody></table></div>}
  </section>;
}
