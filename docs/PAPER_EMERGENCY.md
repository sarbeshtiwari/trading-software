# PAPER emergency controls

Use the existing **Risk** view or authenticated `/api/v1/emergency` API.
The action body contains only `action`, `reason` (at least 10 characters) and
`confirmation`. Neither quantities nor broker order parameters are accepted.

| Action | Exact confirmation | Effect |
| --- | --- | --- |
| `KILL` | `KILL PAPER` | Persistently inhibits all new PAPER entries; does not itself flatten |
| `DISABLE_ENTRIES` | `DISABLE PAPER ENTRIES` | Persistently inhibits entries, retaining position management |
| `FLATTEN` | `FLATTEN PAPER` | Inhibits entries, then asks the active worker to cancel entry orders and close positions safely |
| `CLEAR` | `CLEAR PAPER EMERGENCY` | Clears only emergency flags after fresh critical health checks pass and the running recovered worker reconciles |
| `REVIEW_WORKER` | `REVIEW PAPER WORKER` | Reviews a flat, reconciled worker after fresh health checks, discards unexecuted approved decisions and closes interrupted producer claims as stand-downs |
| `RECOVER_WORKER` | `RECOVER PAPER WORKER` | Restores supervision after an interruption, keeping entries durably disabled and requiring separate review |

An action requires owner authentication and is attributed to that owner in the
audit chain. Flags live in the existing database singleton, are checked during
transactional risk authorization and before dispatch, and are restored at API/
worker startup and worker cycles. Activation blocks locally even if persistence
fails. The state and audit chain must agree. This blocks new admissions; it
cannot recall a broker submission already authorized/in flight.

Flatten serializes with the worker cycle and reuses actual OMS cancellation and
idempotent exits. Responses report persisted order status and remaining quantity
per position or explicit failure. An accepted request is **not** evidence that
positions closed. Stale quotes cannot produce invented fills. An absent worker
returns `WORKER_UNAVAILABLE_FLATTEN_NOT_EXECUTED`; entry inhibition still persists.
Inspect Orders, Positions, Journal and Audit before assuming exposure is gone.

Clear does not clear daily-loss/drawdown/error latches, worker-error state,
interrupted strategy-cycle claims or other health blockers. Empty, failed or
skipped critical health-check sets cannot authorize clearing. There is no LIVE
arming path. A worker requiring operational review stays blocked.

`REVIEW_WORKER` is a separate, explicit action. It refuses open positions or
pending/unknown orders. A transaction marks every unexecuted approved PAPER
proposal blocked, appends reviewed stand-downs to interrupted cycle chains,
persists the cleared worker-error heartbeat, and audits IDs/reason/health results.
It does not replay an interrupted approval, clear emergency/risk latches, or
invent a missing execution outcome. The decision pipeline can import only the
read-only emergency-state checker, never these mutation/recovery controls.

Owner-confirmed flatten may replace a **known terminal cancelled/rejected** exit
after synchronization/reconciliation, for the remaining position quantity only.
Each replacement has a distinct idempotency key and recorded predecessor.
Pending/unknown exits are never blindly replaced. Journals aggregate all exit
fills and link all attempts, including partial fills before cancellation.

After a storage interruption, `RECOVER_WORKER` serializes with the running worker,
persists entry inhibition, reconciles the persisted PAPER broker and independently
checks protection. It may synchronize pending fills or protective exits. It also
finalizes interrupted replacement records without resubmitting replacements and
rechecks expiry warnings using the same services as worker startup. It does
not certify continuous protection during the interruption. A successful recovery
records an owner-attributed receipt and a fresh heartbeat without inventing a
completed strategy cycle. Failure or cancellation leaves execution unready and
entries blocked. A stopped worker must first be started normally.

Recovery is not re-arming: the kill switch, risk latches, discrepancies and worker
review requirement remain. Inspect positions/orders, resolve discrepancies, then
use the separate `REVIEW_WORKER` action when flat and reconciled. Emergency
clearance and any risk reset retain their independent checks. Never edit database
flags to bypass these steps.

Current limitations: PAPER only, no standalone emergency CLI, no automatic
replay of an unavailable-worker flatten request, and existing pending exits are
retained rather than cancelled/replaced. Refused reset attempts do not yet have the same dedicated
audit coverage as successful state changes. These requirements remain partial;
do not alter database safety flags manually to work around them.

Tests verify authenticated confirmation, durable block restoration, blocked
approved orders, real idempotent PAPER flatten, unavailable/stale execution,
state tampering, fresh health-gated clearing and preservation of other blockers.
UI tests verify the actual API request and truthful unavailable execution state.
