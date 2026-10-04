# Opt-in PAPER execution supervisor

The FastAPI lifespan can now start a real APScheduler worker. It is disabled by
default: `PAPER_WORKER_ENABLED=false`. To enable it, configure owner authentication,
starting capital, active risk limits and a verified complete exchange calendar,
then set `PAPER_WORKER_ENABLED=true`. `PAPER_CYCLE_SECONDS` defaults to five seconds.
PAPER remains the mode default; this worker refuses all other modes.

This is an **execution supervisor**, not yet the complete autonomous research/
analysis/strategy loop. It consumes persisted proposals produced by the existing
shared decision pipeline. It never manufactures a proposal or market price.

## Actual operations

- Acquire a host-local OS lock before broker recovery; a second local worker is
  refused. OS ownership is released on exit/crash. Multi-host deployment is not
  supported. Run only one application instance against the PAPER account.
- Recover persisted orders/fills/positions before scheduling work. Unknown orders
  remain blocked and are not blindly resubmitted.
- Determine PRE_MARKET, MARKET_OPEN, INTRADAY, NO_ENTRY_WINDOW, EXIT_WINDOW,
  EOD_RECONCILIATION and MARKET_CLOSE from the existing calendar/session code.
- Refuse entries if the year's calendar is incomplete. The repository's supplied
  2026 calendar is currently incomplete; no exchange dates are invented.
- Serialize cycles, coalesce missed jobs, monitor positions, dispatch at most one
  persisted approval per cycle through fresh preflight, cancel pending entries and
  request MIS square-off at the configured cutoff. Protective exits still use the
  same OMS safety path; a strategy/risk entry block does not suppress exits.
- Enforce a conservative daily submitted-entry cap in the worker. Moving this
  into the pure risk rule contract remains unfinished (INTRA-003 stays partial).
- Read latest persisted LIVE ticks at/before the current timestamp, with actual
  recorded top-of-book depth. No LTP-derived fictitious depth, no historical or
  synthetic fallback. Missing/stale data causes stand-down, not a fill.
- Write a database heartbeat each cycle, including phase, process ID, and failure
  state. `/api/v1/workspace` reports disabled/unavailable/degraded/stale worker
  state. Failures block entries; persisted failure state is restored at restart.

When Groww credentials are configured, the opt-in worker now connects the existing
live market-data provider and drives the registered closed-candle reference
strategy. It builds new account context from the reconciled PAPER broker, loads
active risk limits and point-in-time regime/calendar evidence, and requires
explicit, fresh, audited contract-cost/restriction inputs. No stale DecisionContext
is reused. Without credentials the stored-LIVE-quote execution supervisor remains
available, but reference production is explicitly unavailable.

The authenticated Strategies page can register a reference hypothesis (disabled
initially), enable/disable PAPER only, and publish sourced input evidence. APIs:
`POST /api/v1/strategies/reference/register`, `/strategies/reference/inputs`, and
`/strategies/{strategy_id}/paper`. Reason and owner attribution are audited.
The input schema is in the generated OpenAPI contract; no fee values are supplied
by default. Input publication is not verification of an external cost source.
Missing or expired inputs stand down. A source integration must keep evidence
fresh. Explicit fee estimates now flow through actual fills and journals; see
`PAPER_COSTS.md`. Input publication alone is not verified broker billing.

Each strategy/instrument/origin/closed-bar cycle is durably claimed before work.
A completed claim prevents duplicate signals on later polling or restart. A
claim interrupted before its terminal outcome requires review and blocks entries;
an approval produced just before interruption is not blindly submitted afterward.
Existing approvals cannot overcome missing capital, startup health gates,
incomplete calendars, stale quotes or risk latches.

## Verification and remaining safety work

Tests run actual APScheduler callbacks as well as deterministic phase/cutoff
cycles over the real shared pipeline and durable PAPER broker. They cover host
lock ownership, target exit, square-off, incomplete-calendar stand-down, absent
data, and failure persistence across worker restart. Fixtures are test-only.

Provider-driven worker tests now cover gross and configured-fee target-exit lifecycles,
restart deduplication, interrupted decision production, and unavailable cost
evidence. A watchdog detects changed/missing protection and latches risk; this
does not yet certify continuously protected positions.

No remote service verification or performance claim is implied. Authenticated
PAPER emergency actions and explicit worker review now exist; see
`PAPER_EMERGENCY.md`. FIFO, cost estimates and accounting reconciliation are
integrated, while complete discrepancy recovery, all mandatory notification producers and
continuous real source refresh remain unfinished. Positions conservatively
remain `is_protected=False` until the
full continuous-supervision acceptance contract is verified. Do not manually
remove gate flags to force a trade. Do not deploy this as an unattended trader.

Reference trailing/time/invalidation exits now run over actual open positions,
including durable watermark recovery and explicit analytical-source degradation.
See `PAPER_EXITS.md`; fixed stop/target monitoring is not replaced by analytics.

### Watchdog observations

Startup independently validates open-position protection lineage before starting
the scheduler. Monitoring reconciles and rechecks protection after processing
pending fills, and records quote provenance, quantity, stop/target and an expiry
in the position's hash-chained audit. Missing/changed protection latches risk and
attempts an emergency exit only with valid market data.

The Positions API/UI exposes `watchdog_observation`: unavailable, recent software
check, stale, changed, failed, integrity failure or flat. Evidence expires at the
earlier of quote freshness and three worker intervals. A recent observation is
not a continuously running worker or broker-held stop guarantee; use Monitoring
for worker availability. `is_protected` remains conservatively false. Polling
observations currently accumulate in the audit ledger; retention and scalable
audit-query work remain pending.

### Guarded PAPER entry replacement

The existing Orders page offers cancel/replace only for an acknowledged,
unfilled PAPER entry. Select a separate approved proposal for the same
instrument, strategy, product and direction, enter an owner reason, and type
`REPLACE PAPER ENTRY`. Direct owner price/quantity overrides are not accepted.
`GET /api/v1/orders/{id}/replacement-proposals` lists candidate approvals;
listing is not proof that their evidence is still fresh.

`POST /api/v1/orders/{id}/replace` durably reserves the approval and records
parent/child audit intents before cancellation. Only confirmed zero-fill
cancellation permits the canonical submit path to recheck current evidence,
strategy enablement, sizing/risk approval and preflight. The replacement order
links to the original through `parent_order_id`; prior proposal/fill history is
not rewritten. If risk changes after cancellation, the original may be cancelled
without a replacement. Inspect actual result/error fields rather than treating
HTTP 200 as a successful fill.

Identical request IDs replay recorded results. An interrupted intent is recovered
as `INTERRUPTED_REVIEW_REQUIRED`, never blindly submitted. An existing accepted
replacement is reconciled using its persisted broker reference; an unused
reserved proposal becomes blocked. Failed original cancellations continue through
the existing supervisor. Filled/partial/unknown orders and protective exits are
not eligible. General cross-broker modification remains unimplemented; OMS-004
is partial, not a LIVE execution certification.

### Calendar publication safety

File-loaded yearly completeness requires both `complete: true` and an explicit
timezone-aware `available_at`. A future year-level publication withholds the
year's rows and completeness until that timestamp. Undated legacy rows can
still be inspected, but an undated completeness claim cannot enable trading.
Later individual amendments retain their own availability cutoffs.

Only set year completeness after verifying the required exchange/segment scope
and special-session coverage against official sources. Publication timestamps
must describe when that information was available, not the session date or an
invented historical approval. The shipped production years remain incomplete;
this fix does not grant runtime trading permission or infer Muhurat hours.

An announced special session with unpublished hours is recorded with both
`start` and `end` set to null. On that date, entry scheduling reports
`SPECIAL_SESSION_UNAVAILABLE`, market health reports
`SPECIAL SESSION HOURS UNAVAILABLE`, and session bounds remain unavailable.
Even a weekday does not fall back to regular market hours. Other dates retain
their own calendar behavior. A one-sided, reversed, equal or timezone-bearing
local-time pair is rejected as invalid configuration. Replace null hours only
using a published source with its own availability timestamp; never infer them
from prior years. Existing open positions still require ordinary monitoring.

### Supplied circuit-band checks

PAPER entry preflight records `ENTRY_CIRCUIT_PREFLIGHT` with the proposal,
observation timestamp, supplied bounds and entry price. Inverted, nonfinite,
nonpositive or one-sided bands are rejected. A supplied valid band is inclusive;
an entry or quoted depth outside it is rejected before calling the broker.
The PAPER fill engine independently applies the same check, including after
slippage, so it cannot manufacture a fill outside a supplied band.

Two missing bounds are explicitly recorded as `UNAVAILABLE`, never derived from
previous close or replaced by invented percentages. This is not complete circuit
protection: legacy tick-only and candle-only replay inputs have no circuit
evidence, and historical missing-band coverage remains incomplete. Live execution
remains locked. EXCH-005 stays partial pending complete cross-mode/external
source acceptance; PAPER source recovery and dispatch checks are described below.

The stored quote source now prefers complete provider observations already
persisted by reference ingestion in the hash-chained audit. It checks the chain,
instrument, origin, publication and observation chronology before reconstructing
the quote; no new quote timestamp is invented. Query cutoffs normalize to UTC
for SQLite/PostgreSQL consistency. Recording roundtrips preserve the same bands
and replay exposes them only after publication. Invalid source bands are rejected
at ingestion. Legacy tick-only fallback still has unavailable bands; this is
not a claim that those ticks acquired circuit evidence retroactively.

Market-fed (`LIVE` data origin) entry authorization now refuses absent bands
with `LIVE_SOURCE_CIRCUIT_BAND_UNAVAILABLE`. This concerns data provenance, not
LIVE trading mode: the execution remains PAPER. Historical/replay and isolated
synthetic inputs may retain explicit unavailable-band evidence; their results
must not be presented as exchange-band-verified execution. No instrument-specific
exemption is inferred when a vendor omits limits.

Immediately before dispatch, an entry obtains another fresh quote, verifies its
identity/provenance and circuit bounds, and records an order-linked `DISPATCH`
circuit receipt separately from `PREFLIGHT`. A circuit/freshness/provenance
refusal transitions the unsubmitted order to REJECTED, records the reason, and
reconciles its reservation without calling the broker. Restart does not revive
that order. Protective exits do not acquire this entry-only admission gate;
actual simulated fills still enforce supplied bounds independently. Dispatch
also refuses order price/quantity changes relative to the approved proposal.

### Expiry admission and position warnings

The existing `FNO_EXPIRY_ENTRY_CUTOFF_TIME` setting is validated as local HH:MM
and enforced in shared entry-source validation, including dispatch. F&O expiry
is taken from the instrument record, never inferred from a weekday. Missing or
past expiry is refused; on the expiry date the cutoff is inclusive in IST.
The option strategy's minimum-DTE rule remains an additional independent veto.

Startup and periodic PAPER monitoring record `FNO_EXPIRY_ACTION_REQUIRED` for
open positions requiring attention, and atomically enqueue a critical alert.
Repeated checks reuse durable warning identity. Alerts are produced before
quote-dependent protection checks so missing prices do not hide the expiry
condition. They do not imply an exit, settlement, P&L or external delivery.
Expired pending-entry cancellation and complete expiry lifecycle acceptance
remain pending; EXCH-006 is partial rather than complete.

### Database deadlines and recovery

Database waits are bounded by configurable positive, finite deadlines (maximum
120 seconds each):

| Setting | Default | Scope |
| --- | --- | --- |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | 5 | PostgreSQL connection establishment |
| `DATABASE_COMMAND_TIMEOUT_SECONDS` | 10 | Individual asyncpg commands |
| `DATABASE_POOL_TIMEOUT_SECONDS` | 5 | Waiting for a pooled connection |
| `DATABASE_SESSION_TIMEOUT_SECONDS` | 20 | Entire application session, including checkout and commit |

A session deadline applies to work inside `session_scope`, not just SQL execution.
The validated deadline is captured at engine/session-factory initialization;
changing process environment later does not reconfigure an existing factory.
Timeout/cancellation invalidates the session; it must not be reused or blindly
retried. A timeout during commit is not proof that the commit failed: reconcile
durable state before acting again. PAPER storage failures block new entries and
require explicit owner recovery; recovery does not clear independent safety gates.

The PostgreSQL engine terminates an already-invalidated driver connection instead
of waiting for graceful network cleanup on an unusable connection. This uses the
[SQLAlchemy invalidation event](https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.PoolEvents.invalidate),
which occurs before connection close. Normal healthy connections retain pooling;
SQLite does not receive PostgreSQL driver options. These settings do not certify
continuous protection during an outage or replace reconciliation.
