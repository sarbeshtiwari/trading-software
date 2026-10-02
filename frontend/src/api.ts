import type { components } from './api-schema';

export type Workspace = components['schemas']['WorkspaceView'];
export type RiskLimits = components['schemas']['RiskLimits'];
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export class Api {
  private access = '';
  private refreshing: Promise<boolean> | null = null;
  private async raw(path: string, init: RequestInit = {}) {
    return fetch(`/api/v1${path}`, { ...init, credentials: 'include', headers: {
      'Content-Type': 'application/json', 'X-Requested-With': 'ATS',
      ...(this.access ? { Authorization: `Bearer ${this.access}` } : {}), ...init.headers,
    } });
  }
  async refresh(): Promise<boolean> {
    if (this.refreshing) return this.refreshing;
    this.refreshing = (async () => {
      const response = await this.raw('/auth/refresh', { method: 'POST' });
      this.access = response.ok ? (await response.json()).access_token : '';
      return response.ok;
    })();
    try { return await this.refreshing; } finally { this.refreshing = null; }
  }
  async request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
    let response = await this.raw(path, init);
    if (response.status === 401 && retry && await this.refresh()) response = await this.raw(path, init);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `Request failed (${response.status})`);
    }
    return response.status === 204 ? undefined as T : await response.json() as T;
  }
  async login(username: string, password: string) {
    const body = await this.request<components['schemas']['AccessResponse']>('/auth/login', {
      method: 'POST', body: JSON.stringify({ username, password }),
    }, false);
    this.access = body.access_token;
  }
  quoteStream(instruments: string[], origin: string): WebSocket {
    const url = new URL('/api/v1/market/stream', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    socket.addEventListener('open', () => socket.send(JSON.stringify({ token: this.access, instruments, origin })));
    return socket;
  }
  workspaceStream(): WebSocket {
    const url = new URL('/api/v1/workspace/stream', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    socket.addEventListener('open', () => socket.send(JSON.stringify({ token: this.access })));
    return socket;
  }
  async download(path: string): Promise<Blob> {
    let response = await this.raw(path);
    if (response.status === 401 && await this.refresh()) response = await this.raw(path);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `Export failed (${response.status})`);
    }
    return response.blob();
  }
  async logout() {
    try { await this.request('/auth/logout', { method: 'POST' }); } finally { this.access = ''; }
  }
}
