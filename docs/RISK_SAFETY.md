# Durable risk entry latches

`RiskSafety` stores state transitions in the existing transactional, hash-chained
AuditEvent ledger. No baseline schema changes are needed. Each PAPER/SUPERVISED/
LIVE and data-origin pair has a separate deterministic chain. This is a
single-account deployment model, not multi-account or parallel backtest storage.
Use isolated databases/processes for independent simulations. A process-wide
TradingGate conservatively blocks all entries if any active source blocks them.

Daily loss uses realised plus unrealised loss, with the same stricter absolute/
percentage limits as the pure engine. Reserved or proposed risk does not trip an
actual-loss latch. Equality trips the latch. Equity recovery does not release it.
A fresh observation on a later IST calendar date releases only the daily latch;
the before/after state and source/configuration snapshots are audited atomically.
This does not claim to implement exchange-session scheduling or P&L rollover.

Drawdown and engine-error latches survive date changes and process restarts.
`restore` reconstructs the ledger without clearing latches; every pipeline check
also reads durable state. FastAPI startup restores all origin scopes for the
configured trading mode before running readiness checks. Re-arming is intentionally
unavailable: neither a caller
actor string, changing limits, nor restarting constitutes authentication. No reset
endpoint exists until AUTH-007/SEC-002 and audited authorization are implemented.

Pipeline enforcement occurs before sizing and again under database row locks
before an approved proposal is committed. Engine error verdicts and unexpected
pipeline exceptions latch errors. Invalid risk configuration also latches errors.
An optimistic audit sequence guard rejects stale state writers. SQLite tests
cover a stale-writer interleaving; PostgreSQL concurrency still needs deployment
verification. Approvals remain advisory, not executable order authorizations.

Stale/future/cross-date/backward observations fail closed. Integrity, persistence
or commit failures cannot return an approval. The in-memory storage-failure gate
does not automatically clear on recovery. If storage is entirely unavailable,
durable recording is impossible: restart-time infrastructure/reconciliation gates
must remain closed until reviewed. No cross-restart persistence of an unwritten
failure is claimed. Startup, reconciliation and kill-switch blockers are never
cleared by the safety service. Risk blockers alone allow exits; unrelated blockers
may prohibit them. This is a gate guarantee, not an implemented exit OMS.

New breaches emit CRITICAL local logs and durable CRITICAL audit events. When
notifications are explicitly configured/enabled, those committed events also
enqueue CRITICAL remote notifications. Real remote delivery remains unverified.
Storage failures have separate non-durable degradation alerts. Audit integrity inherits
the ledger's limitations: privileged chain rewriting or suffix deletion without
an external head anchor cannot be detected. Production append-only permissions,
retention and backup policy must protect safety chains from deletion.

Tests include a separate Python process reading the committed SQLite latch,
engine/session recreation, rebound, exact thresholds, IST rollover, other gate
blockers, corruption, storage failures, late latching and misconfiguration.
No broker order is submitted by this component. RISK-005/006/017 remain partial
until monitoring/execution, authenticated re-arm and notification validation satisfy their
full acceptance criteria. PAPER remains default; Groww live is unverified.
