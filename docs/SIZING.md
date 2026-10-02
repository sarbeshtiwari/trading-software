# Deterministic sizing 1.0.0

The sizer does not accept an LLM quantity and cannot execute orders. It takes
validated point-in-time account/market evidence and separately supplied owner
policy. Settings provide capital, per-trade/day risk, gross exposure and margin
buffer; concentration and freshness must be explicitly configured. Missing
starting capital or stale/future evidence fails closed without inventing values.

Sizing capital is the smaller of configured starting capital and current equity.
Requested strategy risk can reduce, never increase, the configured per-trade cap.
Stops are strictly protective. Tick adjustment widens the entry-to-stop distance:
long entry up/stop down, short entry down/stop up. Downstream validation must use
the returned adjusted prices. Estimated risk cost per unit is added to stop risk.

Risk budgets are capped by remaining daily loss budget after actual losses and
reserved open/pending risk. Optional ATR scaling multiplies risk by
`min(1, reference_ATR / current_ATR)` and never increases risk. Optional Kelly
uses `max(0, p - (1-p)/payoff_ratio)`, capped by an explicitly configured fraction
of sizing capital and all ordinary risk caps. Kelly is disabled by default:
estimated win probability/payoff are unstable and must come from point-in-time
validated evidence, never fabricated confidence or in-sample optimism.

Quantity is bounded by risk, buffered margin, remaining gross notional and
instrument concentration; it is always floored to whole lots. The margin buffer
reserves a percentage of available margin, not a leverage estimate. Margin per
unit must be supplied from an appropriate provider; exposure per unit must use
the instrument's conservative notional, not options premium in place of notional.
Zero lots is explicit `BUDGET_BELOW_MIN_LOT`; no minimum-lot upsizing occurs.

Full-precision Decimal strings, evidence sources/timestamps, policy, caps and
result are persisted per proposal in existing sizing records. Replay uses these
strings rather than rounded display columns and verifies the stored result.

Integration limits: trusted account snapshots must include pending reservations;
the current sizer does not acquire account locks or reserve capital itself. Broker
margin validation, option-defined-risk validation, account aggregation, risk veto,
and pre-execution rechecks are separate pending pipeline work. Stop-based planned
risk is not a guarantee against gaps/slippage. No profitability claim is made.
