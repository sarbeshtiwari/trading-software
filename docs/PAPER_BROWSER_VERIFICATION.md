# PAPER browser lifecycle verification

This is an isolated deterministic integration test, not market-performance
evidence. It runs the existing reference worker, ingestion, shared decision/risk
pipeline, PAPER OMS, protection monitoring, costs/FIFO, journal, audit and API.
Headless Edge loads the actual production React bundle over a local HTTP server.
It logs in through the real authentication endpoint; no API responses are mocked
in the browser and no test routes are added to production code.

The browser checks the open position and recent software protection observation,
executed entry/fill, then the exit, gross/charges/net journal, zero exposure,
notification-request records and rendered audit rows. The fixture's hand-checked
gross 888, estimated charges 31.44 and net 856.56 are test expectations, **not
trading results or a profitability claim**.

The target-exit scenario also opens the authenticated Journal lineage, verifies
the available audit-bound PAPER chain, appends an owner note/tag annotation, and
downloads and parses the actual journal JSON artifact. The download retains the
same recorded net P&L and the new annotation. CSV round-trip, rejection records,
date filters, tampering and rollback have API integration coverage described in
`docs/JOURNAL.md`.

The same browser scenario creates an audited contextual correction, verifies its
new version and unchanged net amount, then returns to the original sealed entry
and its annotation. This owner assessment is not a new trade or validation result.

## Run on Windows

Install the project's backend test dependencies and frontend dependencies first.
Microsoft Edge must be installed. The harness uses `playwright-core`; it does not
download a browser. An installed Chrome channel can instead be selected using
`ATS_BROWSER_CHANNEL=chrome`.

```powershell
cd frontend
npm ci
npm run build
cd ../backend
$env:ATS_TEST_BROWSER = '1'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_paper_browser.py -ra
```

The test needs permission to launch a headless browser and a loopback test server.
It allocates a free port, configures that exact test origin in the existing CSRF
allowlist, and closes the browser/server/worker on completion. It uses isolated
SQLite state and test-only credentials, never your account or live broker.
Without the opt-in environment flag, this browser test is explicitly skipped.

## Scope and limits

- Market observations and tariff inputs are deterministic test fixtures. Real
  external market-data/Groww connectivity is not certified.
- The test uses the existing worker fixture for lifecycle setup; production
  FastAPI startup and periodic health checks are tested separately. It does not
  assert that SQLite supplies an external reference clock or that missing live
  dependencies are healthy.
- Browser scenarios prove a successful costed lifecycle, authenticated emergency
  flatten with typed-confirmation gating, duplicate flatten without a new order,
  and stale-quote refusal to fill. A stale exit is explicitly OPEN with the
  position quantity unchanged, not reported as a completed close. Rejection,
  stale-data, missing protection, loss/drawdown, unknown-order, restart and
  other emergency paths have separate service/API integration tests; not all have
  browser-interaction coverage yet.
- Notification requests are visible. External message delivery is unverified.
- Full-day recorded replay, live deployment validation, M1 and M2 remain pending.
