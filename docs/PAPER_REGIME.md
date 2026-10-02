# Provider-fed PAPER regime production

The authenticated `POST /api/v1/strategies/reference/inputs` publication accepts
an optional `inputs.regime_source`. The existing Strategies JSON control uses
that same endpoint. Consult `/openapi.json` for the exact typed schema; there
are no implicit IV, breadth, policy thresholds or calendar values.

Supply these explicitly:

- `index_instrument_id`: an active stored INDEX instrument, not the traded equity.
- `indicators`: ADX, fast/slow MA and realised-volatility periods, one-minute
  `bar_seconds=60`, and the correct annualisation periods for that bar interval.
  Required lookback is bounded to 375 contiguous bars; missing history stands down.
- `policy`: existing deterministic classifier thresholds, freshness, hysteresis
  confirmations and session change cap. Changing a policy with incompatible
  persisted hysteresis state fails closed; automatic policy-state migration is
  not implemented.
- `implied_volatility` and `breadth`: sourced observations with original
  observed/available timestamps, or explicit null when unavailable. Volatility
  uses percent units; breadth uses a fraction. Future evidence is rejected.
- `calendar`: sourced event calendar with knowledge timestamp and coverage, or
  null when unavailable. Empty events only means known-clear within coverage.

For each newly claimed reference decision bar, the worker obtains index candles
through its existing market-data provider, checks closure/order/continuity and
freshness, stores them through CandleStore and records a complete audit snapshot.
The existing technical functions compute ADX/MA/realised volatility, and
RegimeStore persists classification and hysteresis. The resulting regime ID
enters the shared decision context and remains in the journal snapshot;
`regime_at_entry` is also exposed in the journal API.

Absent or stale IV/breadth/calendar evidence produces UNKNOWN or a conservative
safety regime, not a manufactured normal label. Refreshing index prices does
not renew external evidence timestamps. Invalid/future index input records a
stand-down, inhibits the worker and requires explicit review. Completed cycle
claims prevent duplicate evaluation after polling/restart.

Consumers validate the underlying feature timestamps, not just the regime
snapshot timestamp. The shared quantitative/LLM pipeline and strategy gate reject
expired feature evidence. Entry preflight and the final dispatch boundary also
recheck regime and contract evidence and current event-calendar coverage before
any broker submission. Advancing a decision timestamp cannot renew old evidence.
An intent whose evidence expires immediately before dispatch remains unsubmitted;
normal recovery rejects that known never-dispatched intent rather than replaying it.

Dashboard state includes the actual regime ID, underlying, provenance, label,
candidate and observation time. Expired or incomplete features are explicitly
labelled `STALE_OR_INCOMPLETE`, even when the historical label was TRENDING_UP.

Without `regime_source`, the existing point-in-time persisted-regime path remains
available and must still satisfy all freshness and strategy gates. No working
adapter is replaced and no provider fixture is installed in production.

## Limitations

This is per-entry-candidate production, not uninterrupted whole-market analysis.
An open PAPER lifecycle still reserves the worker for protection/exit monitoring.
Automatic external IV/breadth/event publication is not implemented; source-grounded
owner publications remain necessary, including fresh cost/restriction evidence.
Actual Groww index history access and delivery are externally unverified.
No strategy performance, live execution or M1/M2 completion is implied.
