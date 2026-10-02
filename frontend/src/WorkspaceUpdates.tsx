import { useEffect, useState } from 'react';
import { Api } from './api';

export function WorkspaceUpdates({ api, refresh }: { api: Api; refresh: () => Promise<void> }) {
  const [status, setStatus] = useState('HTTP polling; connecting execution updates');
  useEffect(() => {
    let stopped = false;
    let socket: WebSocket | undefined;
    let retry: number | undefined;
    let attempts = 0;
    let received = Date.now();
    function connect() {
      if (stopped || attempts >= 3 || typeof WebSocket === 'undefined') return;
      attempts += 1; received = Date.now();
      try { socket = api.workspaceStream(); }
      catch { setStatus('Execution stream unavailable; HTTP polling'); return; }
      socket.onmessage = event => {
        if (stopped) return;
        try {
          const message = JSON.parse(event.data);
          if (!['WORKSPACE_REFRESH', 'WORKSPACE_HEARTBEAT'].includes(message.type)) throw new Error('Invalid update');
          received = Date.now();
          setStatus('Execution updates connected; other state uses HTTP polling');
          if (message.type === 'WORKSPACE_REFRESH') void refresh();
        } catch { socket?.close(); }
      };
      socket.onclose = event => {
        if (stopped) return;
        socket = undefined;
        setStatus('Execution stream disconnected; HTTP polling');
        void refresh();
        retry = window.setTimeout(() => {
          if (event.code === 4401) void api.refresh().then(ok => { if (ok) connect(); }).catch(() => {});
          else connect();
        }, 1000 * 2 ** (attempts - 1));
      };
    }
    connect();
    const timer = window.setInterval(() => {
      if (socket && Date.now() - received > 12000) {
        setStatus('Execution stream stale; HTTP polling'); socket.close();
      }
    }, 3000);
    return () => { stopped = true; socket?.close(); window.clearTimeout(retry); window.clearInterval(timer); };
  }, [api, refresh]);
  return <p className="muted" aria-label="Workspace update transport">{status}. Events only request authoritative API refreshes; they do not authorize trading.</p>;
}
