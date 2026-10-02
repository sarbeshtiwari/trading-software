import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Api } from './api';
import { NewsArticleImport } from './NewsArticleImport';
import { NewsAcquisition } from './NewsAcquisition';
import { NewsAdvisorySentiment } from './NewsAdvisorySentiment';
import { NewsProductionControls } from './NewsProductionControls';
import type { components } from './api-schema';

type SourceView = components['schemas']['SourceView'];
type SourceList = components['schemas']['SourceList'];

export function NewsSourcesPanel({ api }: { api: Api }) {
  const [sources, setSources] = useState<SourceList>();
  const [runtime, setRuntime] = useState<components['schemas']['NewsRuntimeView']>();
  const [slug, setSlug] = useState('');
  const [configuration, setConfiguration] = useState('');
  const [version, setVersion] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const refresh = useCallback(async () => {
    try {
      const [sourceResult, runtimeResult] = await Promise.all([api.request<SourceList>('/news/sources'), api.request<components['schemas']['NewsRuntimeView']>('/news/runtime')]);
      setSources(sourceResult); setRuntime(runtimeResult); setError('');
    }
    catch (failure) { setRuntime(undefined); setError(String(failure)); }
  }, [api]);
  useEffect(() => { void refresh(); }, [refresh]);
  function select(source: SourceView) {
    setSlug(source.slug); setVersion(source.event_id);
    setConfiguration(source.configuration ? JSON.stringify(source.configuration, null, 2) : '');
    setNotice(source.integrity === 'INVALID' ? 'Configuration integrity failure; mutation is blocked.' : 'Loaded recorded configuration. Editing does not start polling.');
  }
  async function load() {
    setBusy(true); setError('');
    try { select(await api.request<SourceView>(`/news/sources/${encodeURIComponent(slug)}`)); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.request<SourceView>(`/news/sources/${encodeURIComponent(slug)}`, {
        method: 'PUT', body: JSON.stringify({ configuration: JSON.parse(configuration), expected_event_id: version, reason }),
      });
      select(result); await refresh(); setNotice(`Configuration recorded: ${result.event_id}. Scheduler settings are unchanged.`);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section><h2>News source configuration</h2>
    <p>Automatic polling requires server opt-in; enabled does not mean connected or verified. Publisher identities and credibility tiers are owner-supplied, not externally verified.</p>
    <p>News scheduler: {runtime?.status ?? 'UNAVAILABLE'}. Automatic research production: {runtime?.actionable_news ?? 'UNAVAILABLE'}. Last completed cycle: {runtime?.last_cycle ?? 'UNAVAILABLE'}. Resolve instrument research separately for admitted quotations.</p>
    <p>Research worker: {runtime?.research_status ?? 'UNAVAILABLE'}; opt-in: {String(runtime?.research_enabled ?? false)}. Pending/ambiguous calls are never automatically replayed.</p>
    {runtime?.research_results?.map(result => <p key={`${result.article_id}:${result.group_event_id}`}>Research {result.article_id}: {result.status} — {result.code}. Group event: {result.group_event_id}. Admission receipts: {result.admission_ids?.length ?? 0}; current usability requires per-instrument checks.</p>)}
    {error && <p role="alert">NEWS CONFIGURATION UNAVAILABLE — {error}. Displayed records may be stale.</p>}
    {notice && <p role="status">{notice}</p>}
    {!sources ? <p>Loading source configuration…</p> : <>
      {!sources.sources.length && <p>No configured sources. No news has been invented.</p>}
      <ul>{sources.sources.map(source => <li key={source.slug}><button disabled={busy} onClick={() => select(source)}>{source.slug}</button> — {source.integrity}; polling {source.acquisition_status}; enabled {source.configuration ? String(source.configuration.is_enabled) : 'UNAVAILABLE'}</li>)}</ul>
      {sources.has_more && <p>Showing the first 200 sources. Load another source by its exact slug.</p>}
    </>}
    <button disabled={busy} onClick={() => void refresh()}>Refresh source configuration</button>
    <form onSubmit={save}>
      <label>Source slug<input required pattern="[a-z0-9][a-z0-9._-]{0,63}" disabled={busy} value={slug} onChange={event => { setSlug(event.target.value); setVersion(null); setConfiguration(''); setNotice(''); }} /></label>
      <button type="button" disabled={busy || !slug} onClick={() => void load()}>Load source version</button>
      <p>Expected audit version: {version ?? 'NEW OR UNMANAGED SOURCE'}. Existing audited records require their current version.</p>
      <label htmlFor="news-source-configuration">Source configuration JSON</label><textarea id="news-source-configuration" required disabled={busy} value={configuration} onChange={event => setConfiguration(event.target.value)} />
      <p>Required fields: name, publisher_id, tier (1–4), kind (rss/api/filings/regulator), endpoint (HTTPS, no credentials or query), weight (0–1), is_enabled. Tier 1 requires filings/regulator. Do not enter secrets.</p>
      <label>Source configuration reason<input required minLength={10} maxLength={500} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button disabled={busy || !configuration || reason.trim().length < 10}>Save source configuration</button>
    </form>
    <NewsArticleImport key={`${slug}:${version}`} api={api} slug={slug} version={version} />
    <NewsAcquisition key={`acquisition:${slug}:${version}`} api={api} slug={slug} version={version} />
    <NewsAdvisorySentiment api={api} />
    <NewsProductionControls api={api} />
  </section>;
}
