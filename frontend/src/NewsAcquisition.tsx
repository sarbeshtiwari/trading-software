import { useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type PollState = components['schemas']['PollState'];
type PollResult = components['schemas']['PollResult'];

export function NewsAcquisition({ api, slug, version }: { api: Api; slug: string; version: string | null }) {
  const [state, setState] = useState<PollState>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function run(poll: boolean) {
    setBusy(true); setError(''); setState(undefined);
    try {
      if (poll) {
        const result = await api.request<PollResult>(`/news/sources/${encodeURIComponent(slug)}/poll`, { method: 'POST', body: JSON.stringify({ expected_event_id: version }) });
        setState({ status: result.status, result });
      } else setState(await api.request<PollState>(`/news/sources/${encodeURIComponent(slug)}/acquisition`));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="News acquisition">
    <h3>On-demand news acquisition</h3>
    <p>Fetch the configured RSS 2.0 or declared JSON endpoint. Redirects, compressed responses, non-public addresses and IPv6-only endpoints are refused. Acquisition is not corroboration; articles remain UNVERIFIED.</p>
    <button disabled={busy || !slug || !version} onClick={() => void run(true)}>Fetch configured news source</button>
    <button disabled={busy || !slug} onClick={() => void run(false)}>Read news acquisition state</button>
    {busy && <p role="status">News acquisition request pending…</p>}
    {error && <p role="alert">News acquisition unavailable: {error}</p>}
    {state && <p role="status">{state.status === 'DEGRADED' ? 'NEWS SERVICE DEGRADED' : `NEWS ACQUISITION — ${state.status}`}</p>}
    {state?.result && <><p>Attempt: {state.result.attempt_id}</p><ul>{state.result.articles.map(article => <li key={article.article_id}>{article.article_id} — {article.acquisition} — {article.verification_status} — observed {article.observed_at}</li>)}</ul></>}
  </section>;
}
