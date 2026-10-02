# Shared historical/PAPER fill model

Historical replay uses the existing `app.brokers.paper.engine.FillEngine` through
the actual PAPER broker and OMS. There is no separate backtest execution engine
that bypasses sizing, risk, preflight, protection or accounting.

The frozen manifest's `fill_config` specifies `use_depth`, `slippage_bps`,
`partial_fill_probability`, `reject_probability`, `seed` and `latency_ms`.
Recorded input prices are evidence for a simulation, not live broker execution.

- With depth enabled and available, buys consume asks and sells consume bids.
  Limit orders can consume only levels at or better than their limit. Insufficient
  displayed depth produces partial fills, not invented depth. The spread comes
  from the supplied recorded bid/ask snapshot, not an independent synthetic quote.
- Without depth, or with depth disabled, market orders use LTP with adverse
  basis-point slippage, rounded adversely to the stored instrument tick. Limit fills stay at
  their eligible limit rather than inventing price improvement. Increasing this
  fallback slippage does not also penalize depth-based or limit-price fills.
- Seeded rejection and partial-fill probabilities affect actual broker/OMS state.
  A rejected order creates no fill/trade. Partial or pending state at the window
  end cannot be presented as a completed closed-trade result.
- `RECORDED_DEPTH_AT_PUBLICATION` identifies depth-enabled mode, which can fall
  back when a book is absent. Depth-disabled mode now reports
  `RECORDED_LTP_WITH_ADVERSE_SLIPPAGE`. The complete configuration remains frozen
  in the audit-bound manifest; the convention string is not per-fill provenance.

## Verified fixture arithmetic

An isolated reference-strategy fixture buys 111 units at a limit of 100 and exits
against recorded LTP 108. With depth disabled, 0/10/20 bps produce exit prices
108/107.85/107.75 and gross P&L 888/871.35/860.25, respectively, with the fixture's
0.05 tick. Actual persisted
net P&L subtracts recorded charges and declines across these cases. With depth
enabled, the unchanged recorded book determines fills instead of the fallback
slippage knob. These are deterministic test fixtures, not performance evidence.

## Limitations still requiring implementation

`latency_ms` is an injected-clock submission eligibility delay in the PAPER
provider. Each new order and modification records an `eligible_at` timestamp;
restart preserves it, cancellation can prevent the delayed fill, and settlement
uses market data available when execution becomes eligible. Historical replay
advances to these deadlines without wall-clock sleeps or future quotes. This is
not a model of exchange queueing or cancellation/network round-trip latency.
Eligibility is a minimum delay: normal PAPER execution still occurs on a worker
settlement cycle, which may run later. Historical replay visits eligible deadlines
explicitly. A fill-time margin preview uses the same account/fee arithmetic and
refuses exposure-increasing fills that no longer fit available funds; competing
pending orders or changed prices cannot rely on an obsolete submission check.
Risk-reducing exits remain permitted even when account margin is constrained.

A newly filled position may temporarily have only a fresh pre-entry quote.
The reference trailing/invalidation monitor audits that it is waiting for a
post-entry mark; independent fixed stop/target protection remains required.
Stale evidence and timestamp regression after an established exit state continue
to fail closed. The wait never advances the trailing state using pre-entry data.

## Snapshot liquidity conservation

The provider now reconstructs consumed quantities from durable fill evidence.
Each depth fill retains the original bid/ask snapshot, UTC observation time,
data origin, fingerprint and simulation mode. Prices and quantities already filled
on that instrument/side are deducted before the shared engine evaluates another
order. Capacity is shared across products (for example MIS and CNC); bids and asks
have independent budgets. Exhausted levels remain present with zero remaining
capacity, so exhaustion cannot silently switch to the LTP fallback.

Polling, cancellation and process restart never replenish consumed depth. A newer
observation starts a new budget; a changed consumed side at the same observation
time or a regressed observation is refused. Updating an unused opposite side
does not replenish the consumed side. Persisted snapshot fingerprints are checked.
Legacy fills without snapshot evidence require a strictly newer quote than their
execution time on that side; missing legacy execution time fails closed. No source
snapshot or unused capacity is invented for old fills.

Actual historical-worker fixtures poll a 40-unit eligible limit level repeatedly:
without a refreshed snapshot, only 40 of the 111-unit entry can fill; with one
explicit later observation, 80 can fill. Both positions exit through the existing
OMS/account/journal path. This is not a second simulation pipeline.

Resting orders settle in sortable local broker-ID order, not JSON mapping order.
A restart test reverses the persisted mapping and verifies that the original first
order still consumes the scarce capacity. This is deterministic local scheduling,
not a claim to reproduce exchange queue priority.

Exchange queue priority and time-varying market impact are still not modeled.
A newer provider observation is a modeling boundary, not independent proof of
exchange replenishment. The explicit LTP fallback has no verified volume capacity.
No claim of live fill realism, liquidity availability or profitability follows.
Externally observed execution validation remains unverified. BT-003/EXEC-014 and
PAPER-004 have simulation acceptance evidence, not external fill certification.

## Instrument-constrained fills

The execution service resolves the order-linked instrument's lot and tick sizes
from the existing database. The generic production broker factory also installs
a database resolver. New orders persist this snapshot, instrument identity and
capture time; modifications and restart retain it instead of silently rebinding
pending orders to later metadata. Each fill carries the snapshot, and OMS trade
accounting exposes it through the existing workspace API.

Unavailable constraints or legacy pending orders without a snapshot cannot fill
through these application paths. Invalid lot quantities and off-tick quote/order
prices reject safely. Thin levels and seeded partial outcomes fill whole lots;
sub-lot remainders do not invent an executable lot. Snapshot capacity still applies.
The original depth price is retained, including sub-paise ticks. Adverse fallback
rounds buy prices up and sell prices down to the instrument tick, not to a guessed
universal increment.

Fixtures verify durable 25-unit contracts, thin levels, missing metadata, invalid
requests, modifications, legacy recovery, sub-paise depth and adverse partial
fallbacks. Existing historical-worker fixtures and real API lifecycle tests verify
application wiring. These do not certify real derivative execution, contract
eligibility, lot-change corporate actions or external instrument correctness.
Direct low-level engine/broker instances without a resolver retain unconstrained
simulation support; their fill constraint evidence is explicitly null, not verified.
Production factory and OMS construction always install a resolver. Existing
preflight, expiry, safety and risk checks remain independent and authoritative.

## Durable stop activation

A stop crossing is an event, not a condition that must remain true forever.
The shared calculator reports crossing; the PAPER broker persists the original
instrument, side, trigger, observed LTP, provenance and timestamps before filling.
An activated stop-limit remains eligible as a limit after reversal; an activated
stop-market's remaining quantity remains eligible as market. This survives both
no-fill activation and partial-fill restart. Normal capacity, latency, constraints
and margin checks still apply.

Changing an activated trigger/type is refused; explicitly cancel/replace instead.
Quantity/limit modifications retain activation. Pending legacy stops without known
activation state reject rather than invent history. Future/pre-latency observations
cannot activate; invalid persisted evidence fails closed. An activation-storage
failure prevents the fill. The reference strategy's OMS continues to use its
existing monitored synthetic protection; these broker tests do not claim a new
native-stop OMS/UI workflow or real broker protection verification.
