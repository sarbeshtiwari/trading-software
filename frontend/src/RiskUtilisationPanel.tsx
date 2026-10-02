import { useEffect, useState } from 'react';
import { Api } from './api';
import type { components } from './api-schema';

type Utilisation = components['schemas']['RiskUtilisation'];

export function RiskUtilisationPanel({ api, origin }: { api: Api; origin: string }) {
  const [data, setData] = useState<Utilisation>();
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
        const result = await api.request<Utilisation>(`/risk/utilisation?origin=${encodeURIComponent(origin)}`, { signal: controller.signal });
        if (result.origin !== origin || !Array.isArray(result.metrics) || typeof result.status !== 'string') throw new Error('Invalid risk evidence response');
        if (!stopped) { setData(result); setError(''); }
      } catch (failure) {
        if (!stopped) { setData(undefined); setError(String(failure)); }
      } finally { pending = false; window.clearTimeout(timeout); }
    }
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => { stopped = true; controller?.abort(); window.clearInterval(timer); };
  }, [api, origin]);
  return <section aria-label="Risk utilisation"><h2>Risk utilisation</h2>
    <p>Account-level risk observations only, not order authorization. Per-trade and market-dependent checks remain in the decision pipeline.</p>
    {error && <p role="alert">RISK UTILISATION UNAVAILABLE: {error}</p>}
    {!data && !error && <p>Loading risk utilisation…</p>}
    {data && <>
      <p>{data.mode} / {data.origin}: {data.status}</p>
      {data.latch_code && <p role="alert">TRADING DISARMED — {data.latch_code}</p>}
      <p>Observed: {data.observed_at ?? 'UNAVAILABLE'}; configuration: {data.configuration_version ?? 'UNAVAILABLE'}; strategy: {data.strategy_id ?? 'UNAVAILABLE'}</p>
      <p>Audit evidence: {data.evidence_id ?? 'UNAVAILABLE'}</p>
      {data.status === 'AVAILABLE' && data.metrics?.map(metric => <article key={metric.name}>
        <label>{metric.name.replaceAll('_', ' ')}: {metric.used} / {metric.limit}
          {metric.percent !== null && metric.percent !== undefined && <progress aria-label={metric.name} value={Number(metric.percent)} max={100} />}
        </label><span>{metric.percent ?? 'UNAVAILABLE'}%</span>
      </article>)}
    </>}
  </section>;
}
