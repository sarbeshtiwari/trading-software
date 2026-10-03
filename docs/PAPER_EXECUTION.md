# PAPER execution integration

`app.execution.paper.PaperExecution` consumes **persisted pipeline approvals**,
not arbitrary quantities or client-supplied portfolios. Its broker is the existing
PaperBrokerProvider with a database-backed state store. It cannot run in LIVE or
SUPERVISED mode. Run migration 0007 before using it.

## Verified path

`DecisionPipeline.process` -> persisted validation/sizing/risk evidence ->
`PaperExecution.submit(proposal_id)` -> active limits and replayed approval ->
fresh depth/account preflight -> durable intent/reservation -> broker -> fills ->
position -> `monitor_once()` stop/target -> exit -> gross P&L -> immutable completed
journal entry -> audit -> authenticated workspace API -> React views.

Automated fixtures are isolated in `tests/integration/test_paper_execution.py`.
They do not demonstrate a profitable strategy, live prices or Groww execution.
Production code never creates a fixture market stream.

## Safety and operational boundaries

- First call `recover()`; entries additionally require existing startup/health
  gates to be clear. Recovery never clears unrelated gate sources.
- The first slice supports CASH and reserves **one whole trade lifecycle** in the
  database. This is deliberately stricter than the configured position maximum.
  It is not the final multi-position capital-reservation implementation.
- Entries use limit prices from validated proposals; quantity comes from the
  persisted deterministic sizing/risk result. Active configuration and durable
  latches are rechecked immediately before dispatch. Disabled strategies stand down.
- A durable SUBMITTED record precedes the broker call. Timeout/failure uses
  reference lookup only. UNKNOWN persists and blocks entries, even after restart.
  An unresolved intent is never blindly retried. A CREATED intent found at recovery
  is rejected and needs a new decision.
- Partial entry fills create positions only for filled quantity. An exit cancels
  the outstanding entry first and cannot reverse the position. Exit idempotency
  uses a stable position-linked intent. Rejected exits require operator review;
  automated replacement/modify policies remain pending.
- Broker snapshots use optimistic revisions to reject stale writers. After an
  ambiguous broker operation, reload durable broker state rather than treating
  an uncommitted in-memory fill as successful execution.
- Position quantity reconciliation never silently adopts unknown broker orders.
  Full persisted discrepancy resolution, balance reconciliation and distributed
  worker ownership are still pending. This service is not yet deployment-armed.
- Stop and target prices are persisted and `monitor_once()` evaluates actual
  quotes. An opt-in execution supervisor now exists (see PAPER_WORKER.md), but
  continuous-protection acceptance is incomplete: positions remain explicitly
  `is_protected=False`, block further entries and emit a critical monitoring alert.
  A stored stop price alone is not falsely called active protection.
- Daily-loss/drawdown/error latches are enforced at dispatch. Monitoring records
  actual PAPER account marks and losses; exits remain available while entries are
  risk-blocked. Re-arm still requires the authenticated control path and preserves
  daily loss/unrelated blockers.
- P&L separates gross, sourced charge estimates and estimated net. Missing tariffs
  keep journal charges/net unavailable. FIFO now matches actual partial fills and
  survives restart (`PAPER_FIFO.md`); sourced CASH/MIS estimates are integrated
  (`PAPER_COSTS.md`). Broader fee products, overnight/F&O portfolio acceptance and
  externally verified billing remain pending.
- UTC is persisted explicitly so SQLite tests and PostgreSQL compare the same
  instant; API datetime fields carry UTC. Replayed duplicate fills cannot change
  quantity/P&L, and changed fill identity is rejected.

## Defects found through integration

Previous PAPER restore did not restore its saved fills, so a partial order could
not be reconciled after restart. Restore now reloads fill IDs/quantities/times,
order timestamps, simulation configuration and PRNG state, rejecting inconsistent
quantities. Restoring zero cash no longer invents starting capital. Risk-reducing
exits no longer require new-entry margin. Premature average-price rounding lost
five paise in a hand-computed split-fill example; cost basis now retains precision.

## Not yet the user-defined vertical slice

The opt-in supervisor can drive the deterministic reference producer through
provider ingestion and the shared pipeline. The existing Risk UI calls genuine
authenticated PAPER emergency controls; there is no arbitrary order-entry UI.
Journal/preflight/account/fill state is connected through `/api/v1/workspace`.
Full notifications/outbox, EOD, session calendar and all required failure/recovery
cases still need completion. M1/M2 and Groww LIVE remain unverified.

## Owner recovery of a missing position projection

Monitoring exposes PAPER orphan observations with explicitly unavailable accounting.
Acknowledgment records review only. Inspecting a recovery plan replays existing
audited fills/FIFO and original risk approval, retaining any audited trailing stop.
Neither action repairs positions or authorizes trading.

When the original position row alone is missing but its history remains intact:

1. Keep the PAPER worker stopped. Orphan discovery already refuses normal startup;
   leave the authenticated API available for owner inspection.
2. In Monitoring, acknowledge the orphan and inspect the recovery plan. Missing,
   ambiguous, changed or unsealed history must be investigated, not filled in.
3. Supply a meaningful restoration reason and type `RESTORE PAPER POSITION`.
   The API binds the exact reviewed plan hash and rechecks persisted PAPER broker
   orders, quantity, cost basis and FIFO under the broker revision lock. Pending
   orders, another worker, changed evidence or a storage failure refuse restoration.
4. Restoration archives the full adopted observation in its immutable audit chain
   and replaces only that temporary projection with the original position ID.
   Original orders/fills remain untouched; no entry or closing trade is invented.
   The original position's audit chain links to the restoration receipt.
5. Restart the configured PAPER worker for independent reconciliation and protection
   checks. Complete the separate owner discrepancy review. Restoration does not
   clear discrepancies, risk latches or emergency controls, and does not certify
   current marks/protection. Unrealised P&L stays unavailable until a valid mark.

POST `/api/v1/reconciliation/orphans/{id}/restore` requires authentication, the
reviewed plan hash in `expected_head`, `reason`, and the confirmation above.
Retrying the same completed request returns its historical receipt, not fresh
trading authorization. Notification intent and both audit links commit atomically
with reconstruction; external delivery is not verified. Truly missing execution
history and broader multi-process recovery remain unsupported, fail-closed cases.

## Process-crash acceptance scope

`tests/integration/test_process_recovery.py` terminates a real child process after
the production PAPER worker has recovered an open position or pending order. A
second child reopens the same database, acquires the released OS lock, recovers the
same entry identity, processes outstanding fills and exits through the real OMS.
Assertions verify exactly one entry, two lifecycle fills, closure, charge estimates
and net/gross journal linkage. Market data is explicitly synthetic fixture input
persisted through the existing ingestion/audit path; no live broker is contacted.

Both SQLite and isolated PostgreSQL databases on the existing server are covered.
PostgreSQL variants require `ATS_TEST_POSTGRES_URL`; generated test databases are
migrated, populated only with fixture state, and removed after child cleanup.
This verifies application process restart, not server restart, database restart,
network outage, every possible interruption point or time-based PAPER validation.
