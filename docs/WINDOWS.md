# Run locally on Windows without Docker

Use PowerShell. The repository supports Python 3.10+; the installed Vite package
requires Node 20.19+ or 22.12+. Docker is not required to start the application.
You still need reachable PostgreSQL and Redis services for the intended runtime.
These commands do not install services, invent account capital or enable trading.

## Backend

From the repository root:

```powershell
Set-Location backend
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item ..\.env.example .env }
```

For the existing Groww adapter's SDK authentication, also install
`.\.venv\Scripts\python.exe -m pip install -e ".[groww]"`.
The explicit read-only check is
`.\.venv\Scripts\python.exe -m scripts.verify_groww_readonly`.
It reports authentication and index-LTP access separately, prints no credentials
or account data, and sends no order requests. A token alone is not market-data
authorization, a fresh executable quote, or LIVE verification.

If the virtual environment already exists, reuse it. Set private values in
`backend/.env`, not in committed files:

- `DATABASE_URL`: your reachable `postgresql+asyncpg://` database URL. The host
  is normally `127.0.0.1` for a native local database, not a Compose service name.
  Percent-encode special characters in URL credentials.
- `REDIS_URL`: your reachable Redis URL. Redis may run separately or remotely;
  do not replace it with the test-only memory implementation.
- `TRADING_MODE=PAPER`, `BROKER_PROVIDER=paper`, `PAPER_WORKER_ENABLED=false`.
- `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD_HASH` and a cryptographically random
  `JWT_SECRET` of at least 32 bytes. Generate the password verifier locally with
  `.\.venv\Scripts\python.exe -m app.security.hash_password`; it prompts without
  echoing the password. Keep the verifier and JWT secret private.
- `CORS_ORIGINS` matching your actual frontend origin (normally
  `["http://localhost:5173"]`). Do not expose this development server publicly.

`STARTING_CAPITAL=` may remain blank while configuring/inspecting the application.
Blank is treated as unavailable, not zero or invented funds. PAPER execution
still requires explicitly configured capital and matching audited risk limits.

Then run:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use Uvicorn, not `python -m app.main`, to serve the ASGI application. Keep this
terminal open. Authentication and health failures are reported honestly; an API
that starts is not evidence that trading is armed or that Groww is connected.

TimescaleDB is an optional extension in the migration path: when unavailable,
PostgreSQL uses plain candle/tick tables. This does **not** verify hypertables or
retention policies. SQLite is used by isolated tests and is not a substitute for
PostgreSQL production/recovery validation.

## Frontend

In another PowerShell terminal, from the repository root:

```powershell
Set-Location frontend
npm ci
npm run dev -- --host localhost
```

Open the URL printed by Vite. Its `/api` proxy targets `127.0.0.1:8000`. Sign in
with your configured owner credentials. Empty/unavailable data is expected when
no actual observations or trades have been recorded; no demo prices are supplied.

## Troubleshooting and safety

- `starting_capital` decimal parsing with an empty string: update to the checkpoint
  containing the blank-capital fix; no placeholder amount is needed.
- Connection refused: check the configured database/Redis host, port and service.
  Running migrations cannot create a missing PostgreSQL server or database.
- Authentication unavailable: check the owner password verifier and JWT secret.
  Do not paste secrets or complete connection URLs into logs or issue reports.
- A disabled worker is intentional. Before opting in, read `PAPER_WORKER.md`,
  `PAPER_REGIME.md`, `PAPER_COSTS.md` and `PAPER_EMERGENCY.md` and supply genuine
  calendar, source, instrument, capital and risk configuration.

Groww LIVE and external delivery remain unverified. The application is not yet
certified for unattended trading or M1/M2 completion.
