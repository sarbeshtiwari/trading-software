# Shared decision pipeline

`DecisionPipeline.process` accepts advisory JSON/dictionaries or typed quantitative
signals. Both paths pass the same proposal validator, independent sizer and pure
risk engine. No broker, execution or emergency-control module is reachable from
the agents import graph. This is a decision component, not an execution service.

The caller supplies trusted, typed decision context, never fields from model
output: registered strategy/version, stored regime id, instrument, timestamped
portfolio, market/depth, contract margin/notional/cost evidence and owner limits.
The pipeline enforces identity, origin, freshness, regime, registry, event and
LLM dependency gates. PAPER is the only permitted pipeline mode at this stage.
Missing required event coverage and missing sector mapping fail closed.

Signal normalization uses one lot solely for the proposal schema's advisory
quantity check. This is not an order size, market datum or fill; it is never
stored as a quantitative sizing recommendation. The independent sizer computes
the actual candidate quantity from account state for both paths. Only LLM
requests retain `suggested_quantity`, separately from risk-approved quantity.
Absolute risk limits may still veto a percentage-sized candidate rather than
silently relax an owner limit. Strategy reward/risk cannot weaken owner limits.

Proposal, sizing record, full risk decision and considered-candidate outcome are
committed in one transaction. Persistence failure cannot return an approval.
Pre-proposal exclusions (including confidence, invalid evidence and no signal)
retain cycle, instrument, stage, reason and trusted context, without raw rejected
LLM text. Accepted proposal context binds the regime id and all source snapshots.
The existing sizing/risk replay services work with pipeline records unchanged.

The PAPER worker, account reservation, authenticated risk controls, durable
latches, preflight and OMS are now integrated. SUPERVISED/LIVE authorization and
complete derivative execution remain pending. A `RISK_APPROVED` row is **not**
permission to submit an order: execution revalidates current safety state.

Daily entry limits and same-instrument loss cooldown use persisted original
execution history, never agent-supplied counts or P&L. The policy counts entry
intents across strategies per IST date and execution mode/data origin. Pending or
unknown orders reserve quota; retries and partial fills count only once.
Confirmed zero-fill cancellations/rejections do not count. Recent losses use net
P&L from sealed original journals. Unknown net economics or missing history blocks
new entries rather than substituting gross profit. Zero cooldown disables that
window; the exact end timestamp releases it. Exits are not blocked by entry caps.

Risk snapshots retain policy, counted order IDs and journal outcomes for replay.
Preflight refreshes history; final dispatch independently audits another check
before any broker call. Typed workspace preflight responses expose this evidence.

Entry windows also use a shared calendar-based policy at all three boundaries.
The worker uses the same calculation, but its in-memory gate is not the only
enforcement. Frozen evidence includes the session bounds, entry bounds, square-off
time and calendar source. Incomplete calendar coverage blocks entries. Open/close
blackouts use configured nonnegative minute values; square-off accepts local
`HH:MM` only. Special sessions use their configured exchange bounds. If blackout
windows consume the whole session, there is no entry interval. The exact open
blackout end allows entry; the exact close blackout start refuses it. Protective
exits remain available during entry blackouts.

PAPER execution uses the shared strict `DecisionContext.from_snapshot` decoder.
Only declared audit sidecars (validated evidence, regime and advisory sentiment)
are separated from the decision context; unknown extra fields still fail validation.
The original snapshots remain archived and are not silently renewed at execution.

News citations are re-resolved before order creation and again before broker
dispatch. Disabled, stale, changed or unavailable source-policy evidence cannot
reuse an older approval. News/sentiment strategies also require the same policy
version and immutable contributing evidence; a changed policy or contributor set
requires a new decision. Recency-weighted aggregates are checked for current
availability but do not rewrite the decision's historical score. Independent
quantitative strategies do not acquire a news dependency, and protective exits
do not require news to remain available.

This evidence revalidation is not the NEWS-011 instrument-specific halt service.
Durable news-triggered entry blocks and their authenticated clearing remain pending.
