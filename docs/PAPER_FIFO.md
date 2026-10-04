# PAPER FIFO accounting

The PAPER account and OMS share exact Decimal signed-unit FIFO matching.
Each reduction closes the oldest remaining lot, preserving entry/exit IDs,
matched quantity and gross P&L. Multiple entries, partial exits, shorts and
reversals are supported by the accountant. OMS exit authorization still forbids
reversing a position; this accounting capability does not authorize new orders.

Remaining average price is the weighted cost of **remaining FIFO lots**, not the
original average retained after a reduction. Gross realized profit is rounded
cumulatively, preventing partial fills from multiplying rounding differences.
Fill-level realized deltas feed daily risk state before the position is flat.

Broker snapshots retain open lots, exact realized profit and fill sequence.
OMS reconstructs matching from persisted fills with explicit per-position
sequence numbers (not timestamp ties or hash-ID order). Each fill retains FIFO
matches and remaining lots in its accounting breakdown. The workspace API and
existing Orders page expose fills and their accounting lineage.

Restart reconciliation compares quantity, remaining cost basis, gross realized
P&L, known charges and cash. An old open PAPER snapshot without lots fails closed:
it requires a reviewed reconstruction from genuine fill history. The application
does **not** turn its average price into an invented FIFO lot. Legacy current-day
OMS fills without FIFO attribution also require reconstruction before entry-risk
evaluation. Do not delete records or reset capital to bypass this safeguard.

Coverage includes the requested 100@100 + 100@110 scale-in, 50@120 + 150@125
scale-out (gross 3750), shorts/reversals, invalid inputs, cumulative rounding,
corrupted/legacy snapshots, actual partial PAPER exits across restart, journal
and API linkage, and cost-basis discrepancies despite matching quantity.

The scheduled reference worker also runs the sourced fee-estimate lifecycle.
This does not complete the full platform acceptance: derivative-specific rules,
general multi-position/multi-entry execution policy, automatic legacy migration,
continuous real input refresh and broader cross-mode acceptance remain separate
work. PAPER notifications/emergency UI have their own scoped acceptance in the
status ledger. Groww LIVE remains unverified.

## Current exposure evidence

The production PAPER risk snapshot requires a finite positive `last_price` and
an actual `marked_at` for each open position. Marks older than configured
`TICK_STALENESS_SECONDS` are refused with `PAPER_POSITION_MARK_UNAVAILABLE`;
the exact maximum-age boundary remains valid. Missing marks are not replaced by
historical average entry prices and restamped as current risk evidence.

The existing position monitor can restore availability by consuming a valid
current quote and updating the position. This does not change FIFO lots or
fabricate a fill. Closed positions do not require new market marks, and existing
future-state checks remain authoritative. General derivative notional/net exposure
and multi-position acceptance are not claimed complete by this guard.

## Pending entry commitments

Risk snapshots include the unfilled remainder of each nonterminal PAPER entry.
Each commitment requires its audit-bound approved preflight and matching order
identity, quantity, side, limit price, strategy and data origin. Missing or
altered evidence fails closed instead of treating the pending order as free
capacity. Cancelled/rejected/completed orders no longer reserve an unfilled part.

Pending exposure and risk use the sealed per-unit preflight values. Available
risk capital subtracts the pending margin plus fee-risk reserve from broker
available margin; this is a conservative commitment, not a broker charge or fill.
Partial fills move quantity into marked open exposure without counting the same
proposal twice in position limits. Restart rebuilds commitments from durable
orders and receipts. Long-option tests reserve full premium plus configured fee
estimates, not an invented exchange margin.

The existing single-lifecycle execution reservation remains in force. Snapshot
metadata explicitly labels counts as open-and-pending lifecycles and exposure as
open marks plus pending commitments. This is not generalized simultaneous-position
execution, derivative underlying/delta notional, or LIVE reconciliation acceptance.
