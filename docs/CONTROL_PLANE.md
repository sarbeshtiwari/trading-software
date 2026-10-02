# Authenticated PAPER workspace

This is a real API-backed inspection/control application, not yet an end-to-end
trading engine. Orders, positions and accounts remain empty until recorded by
application services. No demo prices or fabricated performance are supplied.

## Run locally

For exact PowerShell commands without Docker, see [Windows setup](WINDOWS.md).

1. Follow the backend database/Redis setup in README. Run `alembic upgrade head`
   from `backend` (migration 0006 adds durable authentication tables).
2. Run `python -m app.security.hash_password` interactively from `backend`.
   Put the resulting Argon2id hash in `DASHBOARD_PASSWORD_HASH` through your
   private environment/secret configuration. Configure a cryptographically random
   `JWT_SECRET` of at least 32 bytes. Do not put either in source control.
3. Start the existing FastAPI entrypoint on `127.0.0.1:8000`.
4. From `frontend`, run `npm ci`, then `npm run dev`. Open the Vite localhost URL.
   Configure `CORS_ORIGINS` to that exact URL. Login uses the configured owner
   username and password; login does not arm trading. TLS is required when exposed.
5. `npm run build` type-checks and builds. `npm test` tests the UI and transport.
   The npm peer resolver compatibility setting avoids an npm 10 optional-peer
   resolver crash; required React/testing peers are explicit dependencies.

Starting capital is never assumed. The Risk page accepts a complete versioned
RiskLimits JSON configuration and a reason. Existing limits are displayed for
editing. Server validation applies; a version conflict is rejected. PAPER only.

## API contract

- POST `/api/v1/auth/login`, `/auth/refresh`, `/auth/logout`; GET `/auth/me`.
- GET `/api/v1/workspace`: typed, bounded, persisted account/position/order/decision,
  strategy, audit and chain summaries. Missing data is UNAVAILABLE, not zero.
- GET `/api/v1/risk`; POST `/risk/configuration`, `/risk/rearm`.
- All other HTTP endpoints (including docs/schema) require Bearer authentication.
  Login/refresh are explicit bootstrap exceptions with CSRF headers/origin checks.
- Access tokens exist only in frontend memory. Rotating refresh tokens use an
  HttpOnly, SameSite strict cookie, Secure when TLS is configured. Only hashes of
  refresh secrets are stored. Logout/replay/key changes invalidate sessions.
- Credential failure counters and lockout survive database-engine/process restart.
- Validation errors do not echo passwords. Successful/denied auth and controls
  have durable audit events. Missing credentials/database fail closed.

`frontend/openapi.json` is exported from `create_app().openapi()` without a running
server; `npm run types` regenerates the client schema. Regenerate after API changes.

Re-arm requires current server-held, hash-verified account evidence and fresh
successful reconciliation. It cannot accept a caller-supplied portfolio or actor.
Daily loss and unrelated gates are preserved. **Until actual OMS reconciliation
is wired, a real empty deployment correctly rejects re-arm.** Risk runtime
enforcement and emergency exits are not claimed complete by these control tests.

## Integration status

Authentication and versioned risk controls are wired into FastAPI. React uses
those actual endpoints. Nine initial navigation views render persisted state:
Dashboard, Positions, Orders, Strategies, Risk, Market/F&O, Decisions, Audit,
Monitoring; the later OMS checkpoint adds a connected Journal view. Order actions,
live indices/charts/Greeks and full strategy performance remain pending. An opt-in
execution supervisor is documented in PAPER_WORKER.md; upstream analysis/strategy
scheduling remains pending. Snapshot P&L is
not labelled live. Groww live execution remains UNVERIFIED. M1/M2 are not complete.
