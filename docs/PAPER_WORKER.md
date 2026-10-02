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
