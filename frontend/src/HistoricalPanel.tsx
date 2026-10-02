import { useEffect, useState } from 'react';
import type { components } from './api-schema';
import { Api } from './api';
import { HistoricalJobs } from './HistoricalJobs';

type Runs = components['schemas']['RunsView'];
type Detail = components['schemas']['RunDetail'];
type Samples = components['schemas']['SamplesView'];
type Trades = components['schemas']['TradesView'];
const shown = (value: unknown) => value === null || value === undefined ? 'UNAVAILABLE' : typeof value === 'object' ? JSON.stringify(value) : String(value);

function EvidenceReview({ api, detail }: { api: Api; detail: Detail }) {
  const [reason, setReason] = useState('');
  const [result, setResult] = useState<components['schemas']['EvidenceReview']>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function review() {
    setBusy(true); setError(''); setResult(undefined);
    try {
      setResult(await api.request(`/backtests/${encodeURIComponent(detail.run.id)}/review`, {
        method: 'POST', body: JSON.stringify({ reason, confirmation: 'REVIEW OOS EVIDENCE' }),
      }));
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  if (!detail.evidence_review) return null;
  return <section aria-label="OOS evidence review"><h3>Predeclared OOS thresholds</h3>
    <p>Research thresholds: {detail.evidence_review.status}. This never approves or enables LIVE trading.</p>
    <p>Policy: {shown(detail.evidence_review.policy)}</p>
    <p>Failed checks: {detail.evidence_review.failures.join(', ') || 'None'}</p>
    <p>Approval blockers: {detail.evidence_review.approval_blockers.join(', ')}</p>
    <label>Evidence review reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <button disabled={busy || reason.trim().length < 10 || detail.catalog_integrity !== 'AUDIT_BOUND'} onClick={() => void review()}>Record audited evidence review</button>
    {result && <p role="status">Review recorded: {result.audit_event_id}; {result.status}; LIVE remains disabled.</p>}
    {error && <p role="alert">Review unavailable: {error}</p>}
  </section>;
}

function Curve({ samples, field, title }: { samples: Samples['samples']; field: 'net_equity' | 'drawdown'; title: string }) {
  const values = samples.map(sample => sample[field] === null ? null : Number(sample[field]));
  const known = values.filter((value): value is number => value !== null && Number.isFinite(value));
  if (!known.length) return <p>{title}: UNAVAILABLE</p>;
  const minimum = Math.min(...known), maximum = Math.max(...known);
  const start = Date.parse(samples[0].observed_at), end = Date.parse(samples[samples.length - 1].observed_at);
  const segments: string[][] = [[]];
  samples.forEach((sample, index) => {
    const value = values[index];
    if (value === null || !Number.isFinite(value)) { segments.push([]); return; }
    const horizontal = 15 + 670 * (Date.parse(sample.observed_at) - start) / (end - start || 1);
    const vertical = 125 - 110 * (value - minimum) / (maximum - minimum || 1);
    segments[segments.length - 1].push(`${horizontal},${vertical}`);
  });
  return <figure><figcaption>{title} — displayed sample page only; gaps are not interpolated</figcaption>
    <svg role="img" aria-label={title} viewBox="0 0 700 140" style={{ width: '100%', maxWidth: 900 }}>
      {segments.filter(segment => segment.length > 0).map((segment, index) => <polyline key={index} points={segment.join(' ')} fill="none" stroke="currentColor" strokeWidth="2" />)}
    </svg><p>Observed range: {minimum} to {maximum}. {samples.length} samples.</p></figure>;
}

export function HistoricalPanel({ api }: { api: Api }) {
  const [runs, setRuns] = useState<Runs>();
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<Detail>();
  const [samples, setSamples] = useState<Samples>();
  const [trades, setTrades] = useState<Trades>();
  const [runOffset, setRunOffset] = useState(0);
  const [sampleOffset, setSampleOffset] = useState(0);
  const [tradeOffset, setTradeOffset] = useState(0);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  async function download() {
    setExporting(true); setExportError('');
    try {
      const blob = await api.download(`/backtests/${encodeURIComponent(selected)}/export`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'simulated-backtest.json';
      document.body.append(anchor); anchor.click(); anchor.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) { setExportError(String(failure)); }
    finally { setExporting(false); }
  }
  useEffect(() => {
    let active = true;
    let pending = false;
    setDetail(undefined); setSamples(undefined); setTrades(undefined); setLoading(true);
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const listed = await api.request<Runs>(`/backtests?offset=${runOffset}`);
        const result = selected ? await Promise.all([
          api.request<Detail>(`/backtests/${encodeURIComponent(selected)}`),
          api.request<Samples>(`/backtests/${encodeURIComponent(selected)}/samples?offset=${sampleOffset}&limit=1000`),
          api.request<Trades>(`/backtests/${encodeURIComponent(selected)}/trades?offset=${tradeOffset}`),
        ]) : null;
        if (active) {
          setRuns(listed); setError('');
          if (result) { setDetail(result[0]); setSamples(result[1]); setTrades(result[2]); }
        }
      } catch (failure) { if (active) setError(String(failure)); }
      finally { pending = false; if (active) setLoading(false); }
    }
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, selected, runOffset, sampleOffset, tradeOffset]);
  return <section><p>SIMULATED historical evidence — not profitability, LIVE approval or external validation.</p>
    <p>{runs?.scope ?? 'Only the configured application database is inspected.'}</p>
    <HistoricalJobs api={api} />
    <HistoricalJobs api={api} experiment />
    {loading && <p role="status">Loading historical evidence…</p>}
    {error && <p role="alert">HISTORICAL DATA UNAVAILABLE — {error}. Previously displayed evidence may be stale.</p>}
    {runs?.runs.length === 0 && <p>No recorded historical runs in this database.</p>}
    <label htmlFor="historical-run">Historical run</label><select id="historical-run" value={selected} onChange={event => { setSelected(event.target.value); setSampleOffset(0); setTradeOffset(0); }}>
      <option value="">Select a recorded run</option>
      {runs?.runs.map(run => <option key={run.id} value={run.id}>{run.id} / {run.status} / {run.progress_pct}%</option>)}
    </select>
    <button disabled={runOffset === 0 || loading} onClick={() => setRunOffset(Math.max(0, runOffset - 50))}>Previous runs</button>
    <button disabled={!runs?.has_more || loading} onClick={() => setRunOffset(runOffset + 50)}>More runs</button>
    {detail && <><h2>{detail.run.status} — {detail.run.progress_pct}%</h2>
      <p>Strategy: {shown(detail.run.strategy_id)} / version {shown(detail.run.strategy_version)}</p>
      <p>{detail.run.data_window_warning} {detail.run.survivorship_note}</p>
      <section aria-label="Historical window adequacy"><h3>Historical window adequacy</h3>
        {detail.window_disclosure ? <>
          <p>{detail.window_disclosure.limitation}</p>
          {detail.window_disclosure.windows.map(window => <p key={window.source_run_id}>
            {window.source_run_id} ({window.role}): {window.start_at} to {window.end_at}; {window.elapsed_seconds} elapsed seconds;
            minimum {window.minimum_calendar_days} calendar days; {window.span_state}; {window.recorded_publications} recorded publications.
            First publication {shown(window.first_publication_at)}; last publication {shown(window.last_publication_at)}.
            Continuous coverage and statistical confidence UNVERIFIED.
          </p>)}
        </> : <p>UNAVAILABLE: this sealed report has no captured window-duration policy. Current settings are not retroactively applied.</p>}
      </section>
      <p>{detail.universe_warning}</p>
      <section aria-label="Universe construction"><h3>Universe construction</h3>
        {detail.universe_disclosure ? <>
          <p>{detail.universe_disclosure.method}: {detail.universe_disclosure.limitation}</p>
          {detail.universe_disclosure.windows.map(window => <details key={window.source_run_id}>
            <summary>{window.source_run_id}: {window.start_at} to {window.end_at}</summary>
            <p>Declared source: {window.source}</p>
            <ul>{window.instruments.map(instrument => <li key={instrument.id}>
              {instrument.trading_symbol} ({instrument.role}); declared known at {instrument.known_at}; source {instrument.source}; active {String(instrument.is_active)}; restricted {String(instrument.is_restricted)}
            </li>)}</ul>
          </details>)}
        </> : <p>UNAVAILABLE: this report has no structured universe disclosure. Historical eligibility and completeness are unverified.</p>}
      </section>
      <p>Internal report integrity: {shown(detail.catalog_integrity)}. This is not external market validation.</p>
      <button disabled={exporting || loading || detail.catalog_integrity !== 'AUDIT_BOUND'} onClick={() => void download()}>Export simulated report JSON</button>
      <p>Export includes the sealed report metrics, curves, trades and universe disclosure; not a full audit backup. Above 10,000 rows per collection or 16 MiB is refused, never truncated.</p>
      {exportError && <p role="alert">Historical export unavailable: {exportError}</p>}
      <EvidenceReview key={detail.run.id} api={api} detail={detail} />
      <p>Recording: {shown(detail.recording_sha256)}. Fill convention: {shown(detail.fill_convention)}</p>
      <p>Reproducibility fingerprints: {detail.reproducibility ? `v${detail.reproducibility.version}; inputs ${detail.reproducibility.inputs_sha256}; outcomes ${detail.reproducibility.outcomes_sha256}` : 'UNAVAILABLE (not recorded for this report)'}. Matching hashes describe recorded inputs/outcomes, not profitability or external verification.</p>
      {!detail.results.length && <p>No completed result snapshot. A RUNNING record is not proof that its child process is still alive.</p>}
      {detail.results.map(result => <div key={result.id}><p>{result.notes}</p><dl>{Object.entries(result).filter(([key]) => key !== 'id' && key !== 'notes').map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{shown(value)}</dd></div>)}</dl></div>)}
    </>}
    {samples && <><Curve samples={samples.samples} field="net_equity" title="Sampled net equity" /><Curve samples={samples.samples} field="drawdown" title="Sampled drawdown fraction" />
      <p>Unavailable samples: {samples.samples.filter(sample => sample.status === 'UNAVAILABLE').length}</p>
      <button disabled={!sampleOffset || loading} onClick={() => setSampleOffset(Math.max(0, sampleOffset - 1000))}>Previous samples</button>
      <button disabled={!samples.has_more || loading} onClick={() => setSampleOffset(sampleOffset + 1000)}>More samples</button></>}
    {trades && <><h2>Recorded closed trades</h2>{!trades.trades.length ? <p>No recorded closed trades.</p> : <div className="table-wrap"><table><thead><tr><th>Symbol</th><th>Quantity</th><th>Entry</th><th>Exit</th><th>Gross</th><th>Charges</th><th>Net</th></tr></thead><tbody>{trades.trades.map(trade => <tr key={trade.id}><td>{trade.trading_symbol}</td><td>{trade.quantity}</td><td>{trade.entry_price}</td><td>{shown(trade.exit_price)}</td><td>{shown(trade.gross_pnl)}</td><td>{shown(trade.charges)}</td><td>{shown(trade.net_pnl)}</td></tr>)}</tbody></table></div>}
      <button disabled={!tradeOffset || loading} onClick={() => setTradeOffset(Math.max(0, tradeOffset - 100))}>Previous trades</button>
      <button disabled={!trades.has_more || loading} onClick={() => setTradeOffset(tradeOffset + 100)}>More trades</button></>}
  </section>;
}
