# Reference PAPER exit policy

The worker now evaluates the pinned `closed-candle-breakout` policy for actual
open PAPER positions. No strategy creates a broker order. All exits call the
same idempotent PAPER execution service, fill accounting, FIFO, fee estimation,
journal, audit and notification path.

- Fixed stop/target evaluation remains first, using validated quote depth.
- The existing pure strategy exit evaluator handles trailing, holding-time and
  invalidation rules. The reference invalidation is a closed close below SMA20.
- A favourable-price watermark, trailing threshold, direction, opening time and
  exit result persist in the position's hash-chained audit. The initial filled
  average price anchors the policy's R-distance; subsequent fills do not reset
  that watermark. Stop/target and the original strategy specification remain
  pinned. Missing/changed persisted trailing state fails closed.
- Restart restores that state rather than restarting the trail at the current
  price. Trailing thresholds cannot silently be loosened through a DB field edit.
- Invalidation is computed at evaluation time from the validated closed-bar
  window. Original candle closure time and the complete ingestion snapshot remain
  linked in audit; this does not relabel candles as newly observed data. Quote
  timestamps retain their original observation time and independent freshness gate.

## Failures and independent exits

Missing analytical input is explicitly `invalidation_checked=false`, never an
invented false invalidation or an affirmative safe-hold decision. Valid quote
evidence can still justify stop/target/trailing/time exits independently. The
watermark is retained even during an analytical outage. If no valid quote exists,
the engine does not manufacture a price or fill.

Every reference exit-monitor failure atomically records a critical audit event,
durable notification request and failed worker heartbeat. A crash before the
normal end-of-cycle heartbeat cannot silently re-enable entries. Existing
authenticated flat-account worker review is still required; delivery errors do
not fabricate success or disable fixed stop/target attempts.

After exits, account snapshots and daily-loss/drawdown observations are refreshed
from actual accounting, including known exit charges. Dashboard exposure/P&L is
not left at the pre-exit snapshot. Positions expose the persisted trailing price;
journals expose their frozen entry regime ID and label.

## Verification and limits

Integration tests cover all three newly wired exit reasons, restored trails,
tampering, missing analytics, independent trailing/time exits during an outage,
fixed stop enforcement, API account state and failure-before-heartbeat restart.
Existing tests cover fixed stops/targets, partial fills, fees/FIFO and broker errors.

Only the existing reference strategy's analytical invalidation is implemented;
this is not an arbitrary-strategy exit interpreter. `is_protected` remains false
until full continuous-supervision acceptance is verified. Software stops cannot
guarantee execution through process/network outages or missing quotes. Market
data and external notification delivery remain unverified. No strategy performance,
Groww LIVE or M1/M2 completion claim is made.
