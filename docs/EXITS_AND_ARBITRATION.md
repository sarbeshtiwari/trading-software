# Exit and arbitration policy

Every strategy specification must now contain an `exit_policy`: positive trailing
risk multiple, maximum holding duration, named invalidation input and maximum mark
age. Missing any field fails validation/registration. Existing stored declarations
without this policy fail closed and require a deliberately registered new version;
no prior approval is inherited or silently migrated.

The shared `Strategy.exit` evaluator cannot be replaced at registration. Strategies
provide timestamped invalidation evidence instead of overriding protective logic.
Priority is STOP, TARGET, TRAILING, TIME, then INVALIDATION. Long/short comparisons
are symmetric; thresholds include equality. Trailing distance is measured from
initial stop risk and a monotonically favourable observed mark. The trailing stop
never weakens the initial stop. No OHLC intrabar sequence or fill is invented.
Future/stale marks, stale invalidation and backwards state updates are rejected.
Returned state must be persisted by the future position-monitoring service.

Arbitration `PRIORITY_V1` selects at most one signal per instrument: lower explicit
integer priority wins, then strategy id/version lexical order. Confidence never
overrides policy. Both opposing and duplicate same-direction losers are logged
with winner/loser ids and content digests. Input order cannot change the result.
Missing priorities, duplicate strategy signals and mixed decision timestamps or
origins fail closed. This resolves signals, not existing positions or orders.

Position monitoring, exit orders and multi-strategy cycle scheduling are still
pending integration. These tests do not constitute strategy profitability or
paper/live execution evidence.
