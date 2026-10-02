import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Readiness = components['schemas']['RuntimeReadinessView'];

export function RuntimeReadinessPanel({ api }: { api: Api }) {
  const [data, setData] = useState<Readiness>();
  const [error, setError] = useState('');
  useEffect(() => {
    let stopped = false;
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await api.request<Readiness>('/system/readiness');
        if (!Array.isArray(result.blockers) || typeof result.entry_gate_open !== 'boolean') throw new Error('Invalid readiness response');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) {
        if (!stopped) { setData(undefined); setError(String(failure)); }
      } finally { pending = false; }
    }
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [api]);
  return <section aria-label="Runtime readiness"><h2>Runtime readiness</h2>
    {error && <p role="alert">READINESS UNAVAILABLE: {error}</p>}
    {!data && !error && <p>Loading runtime readiness…</p>}
    {data && <>
      <p>{data.entry_gate_open ? 'ENTRY GATE OPEN — NOT EXECUTION AUTHORIZATION' : 'TRADING DISARMED — ENTRY GATE BLOCKED'}</p>
      <p>Mode: {data.trading_mode}; worker: {data.worker_state}; execution configured: {data.execution_worker_enabled ? 'enabled' : 'disabled'}</p>
      <p>Health: {data.health_state}; last checked: {data.health_checked_at ?? 'UNAVAILABLE'}</p>
      {data.blockers.map(reason => <p key={reason} className="error">{reason}</p>)}
      {data.calendar_warning && <p className="error">CALENDAR UNVERIFIED: {data.calendar_warning}</p>}
      <p>Stored instruments: {data.instrument_count}; restricted: {data.restricted_instrument_count}. Catalog presence does not mean tradable.</p>
      <p>Instrument source: {data.instrument_snapshot_source ?? 'UNAVAILABLE'}; received: {data.instrument_snapshot_received_at ?? 'UNAVAILABLE'}</p>
      <p>Snapshot audit: {data.instrument_snapshot_audit_verified === null ? 'UNAVAILABLE' : data.instrument_snapshot_audit_verified ? 'VERIFIED' : 'FAILED'}</p>
      <p>Groww LIVE execution: {data.groww_live_execution}</p>
    </>}
  </section>;
}
