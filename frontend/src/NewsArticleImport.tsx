import { useState, type FormEvent } from 'react';
import { Api } from './api';
import { NewsInterpretation } from './NewsInterpretation';
import type { components } from './api-schema';

export function NewsArticleImport({ api, slug, version }: { api: Api; slug: string; version: string | null }) {
  const [text, setText] = useState('');
  const [receipt, setReceipt] = useState<components['schemas']['ArticleReceipt']>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [entities, setEntities] = useState<components['schemas']['AttributionView']>();
  const [entityError, setEntityError] = useState('');
  const [entityBusy, setEntityBusy] = useState(false);
  const [story, setStory] = useState<components['schemas']['StoryView']>();
  const [storyError, setStoryError] = useState('');
  const [storyBusy, setStoryBusy] = useState(false);
  async function loadStory() {
    if (!receipt) return;
    setStoryBusy(true); setStoryError(''); setStory(undefined);
    try {
      setStory(await api.request<components['schemas']['StoryView']>(`/news/articles/${encodeURIComponent(receipt.article_id)}/story`));
    } catch (failure) { setStoryError(String(failure)); }
    finally { setStoryBusy(false); }
  }
  async function loadEntities() {
    if (!receipt) return;
    setEntityBusy(true); setEntityError(''); setEntities(undefined);
    try {
      setEntities(await api.request<components['schemas']['AttributionView']>(`/news/articles/${encodeURIComponent(receipt.article_id)}/entities`));
    } catch (failure) { setEntityError(String(failure)); }
    finally { setEntityBusy(false); }
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setReceipt(undefined); setEntities(undefined); setEntityError('');
    setStory(undefined); setStoryError('');
    try {
      const article = JSON.parse(text);
      setReceipt(await api.request<components['schemas']['ArticleReceipt']>(`/news/sources/${encodeURIComponent(slug)}/articles`, {
        method: 'POST', body: JSON.stringify({ article, expected_event_id: version }),
      }));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Owner article import">
    <h3>Import an observed article</h3>
    <p>Owner import only, not live polling or external verification. Source must be enabled. Imports remain UNVERIFIED and cannot justify trades.</p>
    <p>Supply actual source text and URL, title, body, timezone-aware published_at and explicit data_origin. Do not enter secrets or invent missing news.</p>
    <form onSubmit={submit}>
      <label htmlFor="news-article-json">Article JSON</label>
      <textarea id="news-article-json" required disabled={busy} value={text} onChange={event => setText(event.target.value)} />
      <button disabled={busy || entityBusy || storyBusy || !slug || !version || !text}>Import article observation</button>
    </form>
    {busy && <p role="status">Importing article…</p>}
    {error && <p role="alert">Article import failed: {error}</p>}
    {receipt && <p role="status">{receipt.acquisition} — {receipt.verification_status}; article {receipt.article_id}; audit {receipt.audit_id}; observed {receipt.observed_at}</p>}
    {receipt && <button disabled={entityBusy || busy} onClick={loadEntities}>Read instrument mentions</button>}
    {receipt && <button disabled={storyBusy || busy} onClick={loadStory}>Read story grouping</button>}
    {storyBusy && <p role="status">Loading story grouping…</p>}
    {storyError && <p role="alert">Story grouping unavailable: {storyError}</p>}
    {story && <section aria-label="Story grouping">
      <h4>Story group — {story.story.verification_status}</h4>
      <p>Similarity is not factual corroboration. Group {story.group_id}; audit {story.event_id}; known at {story.known_at}.</p>
      {story.story.ambiguous_group_match && <p>Ambiguous group match: kept separate.</p>}
      <ul>{story.story.members.map(member => <li key={member.article_id}>{member.title}; source {member.source_slug}; publisher {member.publisher_id}; observation {member.article_id}</li>)}</ul>
      {receipt && <NewsInterpretation key={`${receipt.article_id}:${story.event_id}`} api={api} articleId={receipt.article_id} groupEventId={story.event_id} />}
    </section>}
    {entityBusy && <p role="status">Loading attribution…</p>}
    {entityError && <p role="alert">Attribution unavailable: {entityError}</p>}
    {entities && <section aria-label="Instrument mention attribution">
      <h4>Instrument mentions — not tradability or verified news</h4>
      <p>Rule scores are not probabilities. Known at {entities.known_at}; audit {entities.event_id}.</p>
      {!entities.attribution.matches.length && <p>No unambiguous instrument mentions mapped.</p>}
      <ul>{entities.attribution.matches.map((mention, index) => <li key={index}>{mention.anchor}: {mention.instrument_ids.join(', ')}; {mention.rule}; rule score {mention.confidence}</li>)}</ul>
      <p>Excluded ambiguous/low-score mentions: {entities.attribution.excluded.length}</p>
    </section>}
  </section>;
}
