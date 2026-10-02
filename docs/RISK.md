# Deterministic risk engine 1.0.0

`risk.engine.evaluate(proposal, portfolio_state, market_state, config)` is pure:
no clock, network, database, LLM or execution calls. Identical snapshots produce
identical decisions. Every registered check runs; the first failed check is the
binding reason, with all check inputs/results retained. Numeric audit values use
Decimal strings so JSON round trips do not change their types or precision.

Rules cover source/time/identity, per-trade risk, gross exposure, realised plus
unrealised daily loss, reserved risk, peak-equity drawdown, instrument/sector/
underlying concentration, global/strategy position counts, stop side/distance,
tick/lot alignment, defined option loss, reward/risk, margin, depth-based slippage
and instrument blocks. Stops, targets, ratios and costs are validated separately;
an absent stop never bypasses a later rule. Margin and concentration require real
provided state, not assumed leverage or liquidity. Missing option Greeks reject.

The smaller of configured capital/current equity determines monetary limits;
drawdown uses actual peak equity. Absolute currency caps, when supplied, bind if
stricter than percentage/multiple caps. Daily-loss and drawdown thresholds block
at equality; position risk/exposure/margin may equal their cap. Optional supplied
defined option loss widens planned risk, never reduces it below stop-based risk.
Naked/unbounded option risk has no approving path in this component.

Depth must cover the entire quantity. Adverse volume-weighted execution cost is
compared to the configured percentage of planned per-unit risk. Favourable price
movement does not manufacture additional risk budget. Slippage is an estimate,
not a fill guarantee. Crossed or unsorted books fail validation.

`RiskAudit` records full inputs/config/version and every decision outside the
pure engine. Replay recomputes the result and checks stored verdict/rules. This
is reproducibility; the shared decision pipeline also writes hash-chained audit events.

## Outstanding integration work

- This component evaluates **entries**, not exits. `RiskSafety` durably latches
  loss/disarm state and enforces the decision pipeline with local critical audit/
  log events and opt-in remote notification delivery. Authenticated, audited PAPER
  re-arm exists; actual remote channel delivery remains unverified.
  See `RISK_SAFETY.md` for recovery guarantees and limitations.
- Config models are immutable. Authenticated, version-checked PAPER configuration
  activation exists; no LLM mutation route or LIVE configuration permission is
  implied by that owner control.
- Exceptions/malformed snapshots reject and decision-pipeline failures latch
  entry blocking; PAPER preflight/dispatch enforce the shared gates. Broader-mode
  and real broker-boundary validation remain pending.
- Trusted adapters must produce identities, notional, margin, reservations,
  option maximum loss and timestamped portfolio state. These fields are not a
  substitute for instrument/defined-risk proposal validation or broker evidence.
- No execution authorization follows from an approval alone: proposal validation,
  registry/mode/arming gates, sizing, derivative-specific aggregate limits,
  account locking and latest-state preflight are still required.

PAPER remains default; no live connectivity or live order verification is claimed.

## Dashboard account utilisation

The authenticated `GET /api/v1/risk/utilisation?origin=...` reads the same
hash-verified risk-safety portfolio observations produced by the actual PAPER
pipeline and monitor. It does not accept an account from the browser. It requires
matching active configuration, current-session evidence, coherent publication
times and fresh reconciliation without discrepancies. Missing, stale, future,
tampered or configuration-changed evidence yields no metrics, not zero exposure.

Daily loss, loss plus reserved risk, drawdown and gross exposure reuse the engine's
monetary-budget functions, including absolute caps. Global and strategy-specific
open/pending counts are also exposed. Ratios can exceed 100%; a zero budget has an
unavailable percentage. A current observation is not permission to place orders.
Latches remain visible independently of whether the observation is fresh.

The existing Risk view polls this typed API every ten seconds for the selected
data origin, clears failed requests and exposes observation/configuration/audit
identity. Strategy counts apply only to the strategy named in that observation.
Per-candidate risk, concentration, liquidity, margin and other market-dependent
rule utilisation still require fuller dashboard integration; RISK-020 and FE-011
remain partial. No LIVE account values or permission are inferred from PAPER.
