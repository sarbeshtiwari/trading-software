import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Quote = components['schemas']['StoredQuoteView'];

function QuoteRow({ api, identifier, origin, remove }: { api: Api; identifier: string; origin: string; remove: () => void }) {
  const [data, setData] = useState<Quote>();
  const [error, setError] = useState('');
  const [transport, setTransport] = useState('HTTP polling');
  useEffect(() => {
    let stopped = false;
    let pending = false;
    let controller: AbortController | undefined;
    let socket: WebSocket | undefined;
    let connected = false;
    let attempts = 0;
    let received = Date.now();
    let retry: number | undefined;
    setData(undefined); setError('');
    async function load() {
      if (pending || connected) return;
      pending = true;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 8000);
      try {
        const result = await api.request<Quote>(`/market/quotes/${encodeURIComponent(identifier)}?origin=${origin}`, { signal: controller.signal });
        if (result.instrument_id !== identifier || result.origin !== origin) throw new Error('Quote identity mismatch');
        if (!stopped && !connected) { setData(result); setError(''); }
      } catch (failure) { if (!stopped && !connected) { setData(undefined); setError(String(failure)); } }
      finally { pending = false; window.clearTimeout(timeout); }
    }
    function connect() {
      if (stopped || attempts >= 3 || typeof WebSocket === 'undefined') return;
      attempts += 1; received = Date.now();
      try { socket = api.quoteStream([identifier], origin); }
      catch { setTransport('HTTP polling — stream unavailable'); return; }
      socket.onmessage = event => {
        if (stopped) return;
        try {
          const message = JSON.parse(event.data);
          const result = message.quotes?.[0] as Quote;
          if (message.type !== 'QUOTE_SNAPSHOT' || message.quotes.length !== 1 || result.instrument_id !== identifier || result.origin !== origin) throw new Error('Invalid stream observation');
          received = Date.now(); connected = true;
          setTransport('WebSocket'); setData(result); setError('');
        } catch { socket?.close(); }
      };
      socket.onclose = event => {
        if (stopped) return;
        socket = undefined;
        connected = false; setData(undefined); setTransport('HTTP polling — stream disconnected');
        void load();
        retry = window.setTimeout(() => {
          if (event.code === 4401) void api.refresh().then(ok => { if (ok) connect(); }).catch(() => {});
          else connect();
        }, 1000 * 2 ** (attempts - 1));
      };
    }
    void load();
    connect();
    const timer = window.setInterval(() => {
      if (socket && Date.now() - received > 6000) { connected = false; setData(undefined); socket.close(); }
      void load();
    }, 3000);
    return () => { stopped = true; controller?.abort(); socket?.close(); window.clearTimeout(retry); window.clearInterval(timer); };
  }, [api, identifier, origin]);
  const usable = data?.status === 'RECORDED';
  return <tr><td>{identifier}</td><td>{origin}</td><td>{error ? `UNAVAILABLE: ${error}` : data?.status ?? 'LOADING'}</td>
    <td>{usable ? data?.ltp ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td><td>{usable ? data?.change_pct ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td><td>{usable ? data?.volume ?? 'UNAVAILABLE' : 'UNAVAILABLE'}</td>
    <td>{data?.observed_at ?? 'UNAVAILABLE'}</td><td>{transport}</td><td><button onClick={remove}>Remove {identifier}</button></td></tr>;
}

export function QuoteWatchlist({ api, instrument, origin }: { api: Api; instrument: string; origin: string }) {
  const [identifiers, setIdentifiers] = useState<string[]>([]);
  return <section aria-label="Stored quote watchlist"><h3>Stored quote watchlist</h3>
    <p>Audited PAPER ingestion observations only, not verified broker connectivity. WebSocket updates with HTTP polling fallback; stale prices are withheld. Selections last for this view session.</p>
    <button disabled={!instrument || identifiers.includes(instrument) || identifiers.length >= 10} onClick={() => setIdentifiers(previous => [...previous, instrument])}>Watch selected instrument</button>
    {!identifiers.length && <p>No watched instruments. Select a catalog instrument to add it.</p>}
    {identifiers.length > 0 && <div className="table-wrap"><table><thead><tr><th>Instrument</th><th>Origin</th><th>Status</th><th>Stored LTP</th><th>Change %</th><th>Volume</th><th>Observed</th><th>Transport</th><th>Action</th></tr></thead>
      <tbody>{identifiers.map(identifier => <QuoteRow key={`${identifier}:${origin}`} api={api} identifier={identifier} origin={origin} remove={() => setIdentifiers(previous => previous.filter(value => value !== identifier))} />)}</tbody></table></div>}
  </section>;
}
