# Automatic PAPER controls

The system uses existing deterministic controls rather than an LLM override.

| Cause | Control and evidence |
| --- | --- |
| Daily loss / drawdown | RiskSafety's audit-backed latches and transactional critical notices; fresh evidence and existing reset rules remain authoritative. |
| Reconciliation discrepancy | PAPER reconciliation refuses entries, records incident transitions and outbox requests; worker failure review persists across restart. |
| Critical health / feed failure | Health watchdog blocks entries and records transitions; worker feed failures also produce durable incidents. News degradation remains non-critical for independent strategies. |
| Repeated broker rejection | Distinct observed rejection count triggers existing durable DISABLE_ENTRIES state and a critical outbox request. |

`PAPER_BROKER_REJECTION_LIMIT` defaults to 3 (configurable 1–100), counted per
IST day of first broker rejection observation. Both entry and exit rejections
count. Duplicate polls and polling the same rejected order on a later day do not
add to the count. Locally rejected validation/preflight proposals are not broker
rejections. Historical runs capture this configuration.

Order synchronization, rejection evidence, emergency activation and notification
requests commit together. Failure rolls them back; recovery looks up the existing
broker order and records it without resubmission. The durable entry block survives
restart and day rollover. Owner clear still requires the existing fresh health
and reconciliation checks; it does not erase daily rejection history. After clear,
a new rejection can retrigger the limit. Polling an old rejection cannot do so.

Safe exit handling is not disabled. A rejected exit can use the existing explicit
terminal-order replacement path; no automatic successful fill is manufactured.
Original rejection counts/thresholds/order IDs and source audit IDs are inspectable
through Audit; actual notification delivery state is shown independently.

Tests use actual PAPER execution/risk services and deterministic broker rejection
configuration in isolated fixtures. Channel acknowledgements are isolated test
transports, not Telegram/email delivery verification. Groww LIVE, generalized
cross-mode triggers and deployed external health checks remain unverified.
