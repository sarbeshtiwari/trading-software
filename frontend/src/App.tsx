import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Api, ApiError, type Workspace, type RiskLimits } from './api';
import { HistoricalPanel } from './HistoricalPanel';
import { StrategyEvidencePanel } from './StrategyEvidencePanel';
import { StrategyMonitorPanel } from './StrategyMonitorPanel';
import { JournalPanel } from './JournalPanel';
import { PaperSummaryPanel } from './PaperSummaryPanel';
import { AuditPanel } from './AuditPanel';
import { NewsSourcesPanel } from './NewsSourcesPanel';
import { NewsHaltsPanel } from './NewsHaltsPanel';
import { InstrumentBlocksPanel } from './InstrumentBlocksPanel';
import { EventControlsPanel } from './EventControlsPanel';
import { FnoBanPanel } from './FnoBanPanel';
import { RuntimeReadinessPanel } from './RuntimeReadinessPanel';
import { OrderControls } from './OrderControls';
import { RiskUtilisationPanel } from './RiskUtilisationPanel';
import { RiskDecisionPanel } from './RiskDecisionPanel';
import { MarketPanel } from './MarketPanel';
import { EventFailurePanel } from './EventFailurePanel';

const pages = ['Dashboard', 'Positions', 'Orders', 'Strategies', 'Risk', 'Market / F&O', 'Decisions', 'Journal', 'Audit', 'Monitoring', 'Backtests'] as const;
type Page = typeof pages[number];
type RiskState = { limits: RiskLimits | null; status: string; latches: Record<string, { daily_loss: boolean; drawdown: boolean; engine_error: boolean }>; rearm_requires: string };
const shown = (value: unknown) => value === null || value === undefined ? 'UNAVAILABLE' : typeof value === 'object' ? JSON.stringify(value) : String(value);

function Records({ rows }: { rows: object[] }) {
  if (!rows.length) return <p className="empty">No recorded data. No values have been fabricated.</p>;
  const keys = Object.keys(rows[0]);
  return <div className="table-wrap"><table><thead><tr>{keys.map(key => <th key={key}>{key.replaceAll('_', ' ')}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{keys.map(key => <td key={key}>{shown((row as Record<string, unknown>)[key])}</td>)}</tr>)}</tbody></table></div>;
}

function RiskPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const [state, setState] = useState<RiskState>();
  const [error, setError] = useState('');
  const [config, setConfig] = useState('');
  const [reason, setReason] = useState('');
  const [origin, setOrigin] = useState('LIVE');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { const next = await api.request<RiskState>('/risk'); setState(next); setConfig(next.limits ? JSON.stringify(next.limits, null, 2) : ''); }
    catch (failure) { setError(String(failure)); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  async function mutate(kind: 'rearm' | 'configuration') {
    setBusy(true); setError('');
    try {
      const body = kind === 'rearm' ? { origin, reason, confirmation } : { limits: JSON.parse(config), expected_version: state?.limits?.version ?? 0, reason };
      await api.request(`/risk/${kind}`, { method: 'POST', body: JSON.stringify(body) });
      await load(); onChange();
    } catch (failure) { setError(String(failure)); } finally { setBusy(false); }
  }
  return <><EmergencyPanel api={api} onChange={onChange} /><p>{state?.status ?? 'Loading risk state…'}</p>{error && <p role="alert" className="error">{error}</p>}
    <NewsHaltsPanel api={api} onChange={onChange} />
    <InstrumentBlocksPanel api={api} onChange={onChange} />
    <EventControlsPanel api={api} onChange={onChange} />
    <FnoBanPanel api={api} onChange={onChange} />
    <Records rows={Object.entries(state?.latches ?? {}).map(([source, value]) => ({ source, ...value }))} />
    <p className="muted">Re-arm requires fresh, reconciled server account evidence. Daily-loss and unrelated blockers remain in force. No LIVE arming is available.</p>
    <label>Reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <label>Data origin<select aria-label="Data origin" value={origin} onChange={event => setOrigin(event.target.value)}>{['LIVE', 'HISTORICAL', 'REPLAY', 'SYNTHETIC'].map(item => <option key={item}>{item}</option>)}</select></label>
    <RiskUtilisationPanel api={api} origin={origin} />
    <RiskDecisionPanel api={api} />
    <label>Type REARM PAPER RISK<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
    <button disabled={busy || confirmation !== 'REARM PAPER RISK' || reason.length < 10} onClick={() => void mutate('rearm')}>Request authenticated re-arm</button>
    <details><summary>Versioned risk configuration</summary><p>No capital is assumed. Supply an explicitly approved configuration; increment its version when changing it.</p>
      <textarea aria-label="Risk configuration JSON" rows={20} value={config} onChange={event => setConfig(event.target.value)} />
      <button disabled={busy || !config || reason.length < 10} onClick={() => void mutate('configuration')}>Save audited configuration</button></details></>;
}

function EmergencyPanel({ api, onChange }: { api: Api; onChange: () => void }) {
  const phrases = { KILL: 'KILL PAPER', DISABLE_ENTRIES: 'DISABLE PAPER ENTRIES', FLATTEN: 'FLATTEN PAPER', CLEAR: 'CLEAR PAPER EMERGENCY', REVIEW_WORKER: 'REVIEW PAPER WORKER' };
  const [action, setAction] = useState<keyof typeof phrases>('DISABLE_ENTRIES');
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [result, setResult] = useState<object>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { void api.request<object>('/emergency').then(setResult).catch(failure => setError(String(failure))); }, [api]);
  async function submit() {
    setBusy(true); setError('');
    try { setResult(await api.request<object>('/emergency', { method: 'POST', body: JSON.stringify({ action, reason, confirmation }) })); onChange(); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <section><h2>PAPER emergency controls</h2>
    <p>These are real application actions. Blocking entries does not close positions. Flatten requires a running worker and valid market data; inspect each outcome. Clear requires fresh health checks and reconciliation and never clears other risk latches. Worker review requires a flat reconciled account and discards unexecuted approvals and interrupted cycles instead of replaying them.</p>
    {result ? <Records rows={[result]} /> : <p>Emergency state unavailable or loading.</p>}
    {error && <p role="alert" className="error">{error}</p>}
    <label>Emergency action<select value={action} onChange={event => { setAction(event.target.value as keyof typeof phrases); setConfirmation(''); }}>{Object.keys(phrases).map(key => <option key={key}>{key}</option>)}</select></label>
    <label>Emergency reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    <label>Type {phrases[action]}<input value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
    <button disabled={busy || reason.trim().length < 10 || confirmation !== phrases[action]} onClick={() => void submit()}>Execute authenticated emergency action</button>
  </section>;
}

function StrategyPanel({ api, rows, onChange }: { api: Api; rows: Workspace['strategies']; onChange: () => void }) {
  const [instrument, setInstrument] = useState('');
  const [kind, setKind] = useState<'CASH' | 'LONG_OPTION'>('CASH');
  const [reason, setReason] = useState('');
  const [inputs, setInputs] = useState('');
  const [tariff, setTariff] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState('');
  async function mutate(path: string, body: object) {
    setBusy(true); setError(''); setResult('');
    try { await api.request(path, { method: 'POST', body: JSON.stringify({ ...body, reason }) }); setResult('Recorded and audited. Trading still requires all safety gates.'); onChange(); }
    catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  function publish() {
    try { void mutate('/strategies/reference/inputs', { inputs: JSON.parse(inputs) }); }
    catch { setError('Input evidence must be valid JSON. Nothing was submitted.'); }
  }
  function publishTariff() {
    try { void mutate('/strategies/reference/fees', { schedule: JSON.parse(tariff) }); }
    catch { setError('Tariff must be valid JSON. Nothing was submitted.'); }
  }
  return <section><p>Reference strategies are hypotheses, not validated performance. New registrations are disabled.</p>
    <Records rows={rows} />
    <StrategyEvidencePanel api={api} strategies={rows} />
    <StrategyMonitorPanel api={api} strategies={rows} />
    <label>Strategy change reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
    {rows.map(row => <div key={`${row.strategy_id}:${row.version}`}><button disabled={busy || reason.trim().length < 10} onClick={() => void mutate(`/strategies/${encodeURIComponent(row.strategy_id)}/paper`, { version: row.version, enabled: !row.enabled_paper })}>{row.enabled_paper ? 'Disable' : 'Enable'} PAPER {row.strategy_id} {row.version}</button></div>)}
    <h2>Register reference hypothesis</h2>
    <label>Reference instrument class<select value={kind} onChange={event => setKind(event.target.value as 'CASH' | 'LONG_OPTION')}><option value="CASH">Cash equity</option><option value="LONG_OPTION">Long option hypothesis</option></select></label>
    {kind === 'LONG_OPTION' && <p>PAPER long options require audited contract/Greek evidence and all risk gates. Futures and naked shorts remain unsupported; Groww LIVE is unverified.</p>}
    <label>Stored instrument ID<input value={instrument} onChange={event => setInstrument(event.target.value)} /></label>
    <button disabled={busy || !instrument.trim() || reason.trim().length < 10} onClick={() => void mutate('/strategies/reference/register', { instrument_id: instrument, kind })}>Register disabled reference</button>
    <h2>Publish sourced input evidence</h2>
    <p>Provide timestamped contract costs, restrictions and validation policy JSON. Optional regime_source configures closed index-candle analysis with explicit IV, breadth and event-calendar evidence. Missing or stale regime evidence yields UNKNOWN; it is never renewed automatically. Missing or expired contract evidence blocks new reference decisions. This is not a net transaction-cost report.</p>
    <label>Reference input evidence<textarea rows={10} value={inputs} onChange={event => setInputs(event.target.value)} /></label>
    <button disabled={busy || !inputs.trim() || reason.trim().length < 10} onClick={publish}>Publish reference inputs</button>
    <h2>Publish PAPER fee schedule</h2>
    <p>CASH/MIS estimates only. Supply sourced rates, effective dates and rounding rules; no tariff is assumed. Estimates are not broker contract notes.</p>
    <label>Fee schedule JSON<textarea rows={10} value={tariff} onChange={event => setTariff(event.target.value)} /></label>
    <button disabled={busy || !tariff.trim() || reason.trim().length < 10} onClick={publishTariff}>Publish fee schedule</button>
    {error && <p role="alert" className="error">{error}</p>}{result && <p role="status">{result}</p>}
  </section>;
}

export function App({ api: supplied }: { api?: Api }) {
  const [api] = useState(() => supplied ?? new Api());
  const [signedIn, setSignedIn] = useState(false);
  const [checked, setChecked] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [data, setData] = useState<Workspace>();
  const [page, setPage] = useState<Page>('Dashboard');
  const load = useCallback(async () => {
    try { setData(await api.request<Workspace>('/workspace')); setError(''); }
    catch (failure) { setError(String(failure)); if (failure instanceof ApiError && failure.status === 401) { setSignedIn(false); setData(undefined); } }
  }, [api]);
  useEffect(() => { api.refresh().then(setSignedIn).catch(() => setError('API UNAVAILABLE')).finally(() => setChecked(true)); }, [api]);
  useEffect(() => {
    if (!signedIn) return;
    void load(); const timer = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(timer);
  }, [load, signedIn]);
  async function login(event: FormEvent) {
    event.preventDefault(); setError('');
    try { await api.login(username, password); setPassword(''); setSignedIn(true); }
    catch (failure) { setError(String(failure)); setPassword(''); }
  }
  if (!checked) return <main>Checking session…</main>;
  if (!signedIn) return <main className="login"><p className="eyebrow">ATS / OWNER ACCESS</p><h1>Trading workspace</h1>
    <p>Authenticate to inspect system state. Login does not arm trading.</p>{error && <p role="alert" className="error">{error}</p>}
    <form onSubmit={login}><label>Username<input autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} required /></label>
      <label>Password<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /></label><button>Sign in</button></form></main>;
  return <div className="shell"><aside><p className="eyebrow">ATS / CONTROL ROOM</p><h2>Trading workspace</h2><nav>{pages.map(item => <button className={page === item ? 'active' : ''} key={item} onClick={() => setPage(item)}>{item}</button>)}</nav>
    <button onClick={() => void api.logout().catch(failure => setError(String(failure))).finally(() => { setSignedIn(false); setData(undefined); })}>Sign out</button></aside>
    <main><header><div><p className="eyebrow">{data?.trading_mode ?? 'MODE UNAVAILABLE'}</p><h1>{page}</h1></div><button onClick={() => void load()}>Refresh</button></header>
      <div className={data?.new_entries_allowed ? 'status' : 'status blocked'}>{data ? (data.new_entries_allowed ? 'Entry gate clear — not proof of execution readiness' : `ENTRIES BLOCKED — ${data.blockers.join('; ')}`) : 'Loading application state…'}</div>
      <p className="muted">Groww LIVE execution: {data?.broker_verification ?? 'UNAVAILABLE'}. Persisted snapshots are not a live market feed. P&amp;L is gross unless explicitly labelled net. Charges may be incomplete; estimates are not broker bills.</p>
      {error && <p role="alert" className="error">API ERROR — {error}. Displayed data may be stale.</p>}
      {data && <><p className="muted">Response time: {data.generated_at}</p>
        {page === 'Dashboard' && <><div className="cards">{[['Account', data.account_status], ['Equity', data.account?.equity], ['Realised P&L (snapshot)', data.account?.realised_pnl], ['Unrealised P&L (snapshot)', data.account?.unrealised_pnl], ['Exposure (snapshot)', data.account?.gross_exposure], ['Regime', data.regime ? `${data.regime.label} / ${data.regime_status}` : data.regime_status]].map(([name, value]) => <article key={String(name)}><span>{name}</span><strong>{shown(value)}</strong></article>)}</div><Records rows={data.regime ? [data.regime] : []} />
          <h2>Recent decisions</h2><Records rows={data.decisions.slice(0, 8)} /><h2>System components</h2><Records rows={data.components} /></>}
        {page === 'Positions' && <><p>Watchdog observations are expiring software checks, not broker-held stop guarantees. Check Monitoring for current worker availability.</p><Records rows={data.positions} /></>}{page === 'Orders' && <><OrderControls api={api} orders={data.orders} mode={data.trading_mode} onChange={() => void load()} /><Records rows={data.orders} /><h2>Persisted fills and FIFO attribution</h2><Records rows={data.fills ?? []} /></>}
        {page === 'Strategies' && <StrategyPanel api={api} rows={data.strategies} onChange={() => void load()} />}
        {page === 'Decisions' && <><h2>Advisory decisions</h2><Records rows={data.decisions} /><h2>Proposal provider receipts</h2><p>Fallback is rule-derived, not a Claude response or order authorization. Missing token counts are unavailable, not zero. Remote costs are owner-tariff estimates, not provider invoices. Unknown costs retain their budget reservation.</p><Records rows={data.advisory_calls ?? []} /><h2>Advisory budget (UTC day)</h2><Records rows={data.advisory_budget ? [data.advisory_budget] : []} /><h2>Execution preflight</h2><Records rows={data.preflights} /></>}
        {page === 'Journal' && <JournalPanel api={api} />}
        {page === 'Backtests' && <HistoricalPanel api={api} />}
        {page === 'Audit' && <AuditPanel api={api} />}
        {page === 'Monitoring' && <><RuntimeReadinessPanel api={api} /><EventFailurePanel api={api} /><Records rows={data.components} /><h2>Durable notification outcomes</h2><p>A channel acknowledgement is not independently verified message delivery. Pending/failed notices do not imply a trading failure.</p><Records rows={data.notifications ?? []} /><PaperSummaryPanel api={api} /></>}
        {page === 'Market / F&O' && <><MarketPanel api={api} /><p>Recorded option-chain summaries; live indices, Greeks and futures views remain unavailable until connected.</p><Records rows={data.chains} /><NewsSourcesPanel api={api} /></>}
        {page === 'Risk' && <RiskPanel api={api} onChange={() => void load()} />}</>}
    </main></div>;
}
