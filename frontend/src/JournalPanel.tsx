import { useEffect, useState, type FormEvent } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type JournalList = components['schemas']['JournalList'];
type JournalDetail = components['schemas']['JournalDetail'];
const emptyFilters = { start: '', end: '', instrument_id: '', strategy_id: '', kind: '', outcome: '' };
const shown = (value: unknown) => value === null || value === undefined ? 'UNAVAILABLE' : typeof value === 'object' ? JSON.stringify(value) : String(value);

export function JournalPanel({ api }: { api: Api }) {
  const [draft, setDraft] = useState(emptyFilters);
  const [filters, setFilters] = useState('');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState('');
  const [data, setData] = useState<JournalList>();
  const [detail, setDetail] = useState<JournalDetail>();
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [actionError, setActionError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [note, setNote] = useState('');
  const [tags, setTags] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [assessment, setAssessment] = useState('');
  const [correctionReason, setCorrectionReason] = useState('');
  useEffect(() => {
    let active = true, pending = false;
    setData(undefined); setError('');
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await api.request<JournalList>(`/journal?${filters}&offset=${offset}&limit=25`);
        if (active) { setData(result); setError(''); }
      } catch (failure) { if (active) setError(String(failure)); }
      finally { pending = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, filters, offset, refresh]);
  useEffect(() => {
    let active = true, pending = false;
    setDetail(undefined); setDetailError(''); setMessage(''); setActionError(''); setNote(''); setTags(''); setReason(''); setAssessment(''); setCorrectionReason('');
    async function load() {
      if (!selected || pending) return;
      pending = true;
      try {
        const result = await api.request<JournalDetail>(`/journal/${encodeURIComponent(selected)}`);
        if (active) { setDetail(result); setDetailError(''); }
      } catch (failure) { if (active) setDetailError(String(failure)); }
      finally { pending = false; }
    }
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, selected, refresh]);
  function apply(event: FormEvent) {
    event.preventDefault(); setOffset(0); setSelected('');
    setFilters(new URLSearchParams(Object.entries(draft).filter(([, value]) => value !== '')).toString());
    setRefresh(value => value + 1);
  }
  async function annotate(event: FormEvent) {
    event.preventDefault(); setBusy(true); setActionError(''); setMessage('');
    try {
      await api.request(`/journal/${encodeURIComponent(selected)}/annotations`, { method: 'POST', body: JSON.stringify({
        note, tags: tags.split(',').map(tag => tag.trim()).filter(Boolean), reason,
      }) });
      setDetail(await api.request<JournalDetail>(`/journal/${encodeURIComponent(selected)}`));
      setNote(''); setTags(''); setReason(''); setMessage('Annotation recorded and audited. Economics unchanged.');
    } catch (failure) { setActionError(String(failure)); }
    finally { setBusy(false); }
  }
  async function download(format: 'csv' | 'json') {
    setBusy(true); setActionError('');
    try {
      const blob = await api.download(`/journal/export/${format}?${filters}`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = `journal.${format}`;
      document.body.append(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) { setActionError(String(failure)); }
    finally { setBusy(false); }
  }
  async function correct(event: FormEvent) {
    event.preventDefault();
    if (!detail) return;
    setBusy(true); setActionError('');
    try {
      const body = { expected_version: detail.entry.version, reason: correctionReason,
        ...(detail.entry.kind === 'TRADE' ? { plan_adherence: assessment } : { rejection_detail: assessment }),
      };
      const result = await api.request<JournalDetail>(`/journal/${encodeURIComponent(selected)}/corrections`, { method: 'POST', body: JSON.stringify(body) });
      setSelected(result.entry.id); setRefresh(value => value + 1);
    } catch (failure) { setActionError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section aria-label="Trade journal">
    <p>Persisted journal only. Rejections are not trades. Correction versions are not additional trades; never sum duplicate versions. Net P&amp;L requires every fill to have a configured fee schedule; charges remain estimates. Refreshes every 10 seconds.</p>
    <form onSubmit={apply}>
      {(['start', 'end', 'instrument_id', 'strategy_id'] as const).map(key => <label key={key}>{key.replaceAll('_', ' ')} (journal)<input type={key === 'start' || key === 'end' ? 'date' : 'text'} value={draft[key]} onChange={event => setDraft({ ...draft, [key]: event.target.value })} /></label>)}
      <label htmlFor="journal-kind">Journal kind</label><select id="journal-kind" value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value })}><option value="">All kinds</option><option>TRADE</option><option>REJECTION</option><option>ORDER_ACTION</option></select>
      <p>Order actions record cancellation attempts, not closed trades. Their P&amp;L remains unavailable; any actual fills retain their own position accounting.</p>
      <label htmlFor="journal-outcome">Journal outcome</label><select id="journal-outcome" value={draft.outcome} onChange={event => setDraft({ ...draft, outcome: event.target.value })}><option value="">All outcomes</option>{['WIN', 'LOSS', 'FLAT', 'UNAVAILABLE'].map(value => <option key={value}>{value}</option>)}</select>
      <button disabled={busy}>Apply journal filters</button>
    </form>
    <button disabled={busy} onClick={() => setRefresh(value => value + 1)}>Refresh journal</button>
    <button disabled={busy} onClick={() => void download('csv')}>Export journal CSV</button>
    <button disabled={busy} onClick={() => void download('json')}>Export journal JSON</button>
    <p>Exports include all matching records, up to 2,000 / 16 MiB; larger exports are refused, never truncated. CSV cells contain JSON values for lossless, spreadsheet-safe parsing. Dates use Asia/Kolkata.</p>
    {error && <p role="alert">Journal unavailable or stale: {error}</p>}
    {actionError && <p role="alert">Journal action failed: {actionError}</p>}
    {!data && !error && <p role="status">Loading journal...</p>}
    {data && <><p>{data.scope}</p>{!data.entries.length ? <p>No journal entries match these filters.</p> : <div className="table-wrap"><table><thead><tr>{['ID', 'Version / role', 'Kind', 'Symbol', 'Strategy', 'Exit / rejection', 'Gross P&L', 'Charges', 'Net P&L', 'Integrity', 'Lineage'].map(label => <th key={label}>{label}</th>)}</tr></thead>
      <tbody>{data.entries.map(row => <tr key={row.id}><td>{row.id}</td><td>{row.version} / {row.record_role}</td><td>{row.kind}</td><td>{shown(row.trading_symbol)}</td><td>{shown(row.strategy_id)}</td><td>{shown(row.rejection_code ?? row.exit_reason)}</td><td>{shown(row.gross_pnl)}</td><td>{shown(row.charges)}</td><td>{shown(row.net_pnl)}</td><td>{row.integrity}</td><td><button disabled={busy} onClick={() => setSelected(row.id)}>Inspect {row.id}</button></td></tr>)}</tbody></table></div>}
      <button disabled={busy || offset === 0} onClick={() => { setOffset(Math.max(0, offset - 25)); setSelected(''); }}>Previous journal page</button>
      <button disabled={busy || !data.has_more} onClick={() => { setOffset(offset + 25); setSelected(''); }}>Next journal page</button></>}
    {selected && <section aria-label="Journal lineage"><h2>Journal lineage: {selected}</h2>
      {detailError && <p role="alert">Lineage unavailable or stale: {detailError}</p>}
      {!detail && !detailError && <p role="status">Loading lineage...</p>}
      {detail && <><p>Lineage: {detail.lineage_status}; integrity: {detail.entry.integrity}; mode: {detail.entry.mode}</p>
        <p>Journal version {detail.entry.version}: {detail.entry.record_role}. {detail.correction_scope}</p>
        {detail.entry.record_role === 'OWNER_CONTEXT_CORRECTION' && <p>OWNER ASSESSMENT — NOT NEW EXECUTION OR STRATEGY APPROVAL EVIDENCE.</p>}
        <button disabled={busy || selected === detail.entry.revision_root_id} onClick={() => setSelected(detail.entry.revision_root_id)}>Inspect original journal</button>
        <button disabled={busy || selected === detail.entry.latest_version_id} onClick={() => setSelected(detail.entry.latest_version_id)}>Inspect latest journal version</button>
        {!!detail.revisions?.length && <details><summary>Audited version history</summary><pre>{JSON.stringify(detail.revisions, null, 2)}</pre></details>}
        {!!detail.gaps.length && <p role="alert">Recorded gaps: {detail.gaps.join(', ')}</p>}
        {(['entry', 'candidate', 'proposal', 'sizing', 'risk_decisions', 'orders', 'fills', 'order_events', 'position', 'regime', 'audit'] as const).map(key => <details key={key}><summary>{key.replaceAll('_', ' ')}</summary><pre>{shown(detail[key])}</pre></details>)}
        <h3>Owner annotations</h3>{detail.entry.annotations.length ? detail.entry.annotations.map(item => <article key={item.id}><p>{item.note}</p><p>{item.tags?.join(', ')} — {item.author} — {item.created_at}</p></article>) : <p>No annotations recorded.</p>}
        <form onSubmit={event => void annotate(event)}>
          <label>Journal note<textarea value={note} maxLength={4000} onChange={event => setNote(event.target.value)} /></label>
          <label>Journal tags (comma separated)<input value={tags} onChange={event => setTags(event.target.value)} /></label>
          <label>Journal annotation reason<input value={reason} maxLength={500} onChange={event => setReason(event.target.value)} /></label>
          <button disabled={busy || !!detailError || !note.trim() || reason.trim().length < 10}>Add audited annotation</button>
        </form></>}
      {detail && detail.entry.kind !== 'ORDER_ACTION' && <details><summary>Append contextual correction</summary>
        <p>Only owner plan-adherence assessment or rejection explanation can be corrected. Original economics, fills and audit seals stay unchanged. Corrected trades require new evidence review; this action never grants trading permission.</p>
        <form onSubmit={event => void correct(event)}>
          {detail.entry.kind === 'TRADE' ? <><label htmlFor="journal-adherence">Corrected plan adherence</label><select id="journal-adherence" value={assessment} onChange={event => setAssessment(event.target.value)}><option value="">Select assessment</option>{['FOLLOWED', 'DEVIATED', 'UNAVAILABLE'].map(value => <option key={value}>{value}</option>)}</select></> : <label>Corrected rejection explanation<textarea maxLength={2000} value={assessment} onChange={event => setAssessment(event.target.value)} /></label>}
          <label>Journal correction reason<input maxLength={500} value={correctionReason} onChange={event => setCorrectionReason(event.target.value)} /></label>
          <button disabled={busy || !!detailError || selected !== detail.entry.latest_version_id || !assessment.trim() || correctionReason.trim().length < 10}>Create audited journal version</button>
        </form>
      </details>}
    </section>}
    {message && <p role="status">{message}</p>}
  </section>;
}
