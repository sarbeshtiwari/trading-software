# Application status

Updated 2026-10-02. This is the short owner-facing status; detailed evidence is
in `IMPLEMENTATION_STATUS.md` and the requirement ledger.

## Bottom line

The application has a tested end-to-end **simulated PAPER** workflow and an
API-backed React dashboard. It is **not production-complete or LIVE-ready**.

| Contract status | Requirements |
|---|---:|
| Implemented, tested and integrated | 229 |
| Partial | 129 |
| Implemented but not fully verified | 10 |
| Not started | 165 |
| Total | 533 |

About 43% of requirements meet the strict verified ledger status. This is not a
weighted estimate of development effort or an assertion of production readiness.

## What works in verified application tests

- Recorded market data -> analysis -> equity/long-option reference strategy ->
  proposal validation -> deterministic sizing/risk -> PAPER OMS/fill -> protected
  position -> exit -> FIFO/cost estimates -> journal/audit -> API/dashboard.
- Authentication, risk latches, emergency/review controls, rejection paths,
  restart/recovery cases, notification outbox and session orchestration.
- Isolated historical execution and chronological walk-forward/OOS reports,
  published through authenticated APIs and the real browser UI.
- Owner-confirmed historical cancellation, child-process cleanup and durable
  interruption, without releasing unknown-owner reservations.

These are software/integration results. They are not real-market performance,
profitability, externally delivered notifications or M2 PAPER validation evidence.

## Actual local environment checks

On 2026-10-02:

- Groww credentials are configured; their values are never published.
- Installed missing official Groww SDK 1.5.0; package dependency check passes.
- Real Groww token authentication succeeds. Read-only NIFTY LTP fails with
  **HTTP 403**, classified as a JSON broker failure, not an HTML response.
  Market-data authorization/connectivity remains unresolved; the
  exact broker/network cause has not been established. No order requests sent.
- A bounded check through official Groww SDK 1.5.0 also returns HTTP 403 for LTP;
  requested nonsecret account/authorization status from owner. No further retries
  are needed without new evidence. This is not an adapter-only failure claim.
- Owner confirmed the existing Docker services are running. Both containers are
  healthy (TimescaleDB/PostgreSQL 16 and Redis 7), bound to localhost. Application
  configuration now passes actual PostgreSQL `SELECT 1` and Redis `PING` checks.
  Earlier connection failures are resolved; no additional installation is needed.
- Applied migrations through `0016_candle_time_index`; the real local database
  reports that migration head.
- Actual PAPER backend startup succeeds with execution disabled; unauthenticated
  HTTP access returns 401. Audited real public instrument import now stores
  98,750 contracts; retained CSV checksum and audit chain verify. The backend
  observes the catalog and clears its empty-master blocker. Market-data readiness
  still blocks trading; no worker or live orders were enabled.
- Private local configuration currently selects SUPERVISED/Groww, with the
  worker disabled. It has not been silently changed or armed. Repository defaults
  remain PAPER. Credentials do not authorize autonomous real-money orders.

## Verification and source publication

- Latest full baseline (`ccc8ce0`): **1609 passed, 0 failed, 5 PostgreSQL-only skipped**, including real-browser acceptance.
- Latest affected research regression: **800 passed, 0 failed, 0 skipped**.
- Readiness regression: **792 passed, 0 failed, 0 skipped**.
- Frontend: **41 passed**; production build succeeds; actual Edge lifecycle tests
  cover PAPER, historical/OOS and research cancellation.
- Monitoring now includes API-backed runtime readiness: worker enablement, entry
  blockers, calendar coverage, stale health, catalog counts and snapshot audit.
  Real Edge verification passed across target, emergency and stale-exit paths.
- Orders now supports authenticated, audited PAPER entry cancellation, including
  partial fills, idempotent replay and recovery. Actual Edge cancellation passed;
  affected backend regression: **777 passed**. Bulk-entry cancellation now also
  persists all targets atomically and recovers interrupted batches; its affected
  regression is **767 passed**, with two actual single/bulk browser cases passing.
  General cancellation and risk-revalidated modification acceptance remain.
- Replaced three placeholder PostgreSQL checks. Real testing exposed and fixed
  a concurrent overfill race through migration 0015. Schema-related checks:
  **26 passed**, including six real PostgreSQL checks. Broader affected regression:
  **794 passed / 4 opt-in skips**, separately covered against PostgreSQL.
- Audit immutability, tick retention and fill constraints are now verified against
  Docker PostgreSQL. Fresh installation and `alembic check` now pass on an isolated
  empty database and the existing owner database; intentional drift is detected.
  Latest affected backend regression: **801 passed / 4 PostgreSQL-only skipped**,
  with those PostgreSQL guarantees separately verified against the real server.
- GitHub: `https://github.com/sarbeshtiwari/trading-software`, branch `main`.
  Initial published snapshot: `b8a1572`, equivalent to verified local `07c5695`.
- Publication uses the owner's configured identity without co-author trailers.
  Older development history is preserved locally on `master`; it was not pushed.
  Private configuration and unrelated uncommitted owner files were excluded.

## Priority order from now

1. Verify database migrations and resolve Groww read-only market-data access;
   expose actionable readiness rather than treating passing fixtures as deployment.
2. Run the existing application in PAPER against authorized real inputs, including
   source/calendar/risk/capital readiness, monitoring and dashboard verification.
3. Close remaining PAPER accounting/reconciliation/protection/notification gaps.
4. Finish historical/strategy validation and AI/supervised integration gaps.
5. Complete learning/reporting/security/deployment acceptance and final audit.
6. Consider LIVE only after genuine broker verification and all owner-controlled
   compliance, reconciliation, validation and arming gates are satisfied.

M1 and M2 are **not complete**. Missing services/data will never be replaced by
fake connections, prices, fills or performance claims.
