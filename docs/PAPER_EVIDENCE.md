# PAPER evidence eligibility

The Strategies view exposes the real application's coverage and journal evidence.
It does not approve LIVE trading. PAPER remains default; external feed verification,
time-based operational validation and Groww LIVE validation are separate.

## Declare policy before observation

An authenticated owner selects a registered strategy/version and publishes explicit
JSON with `version`, `minimum_sessions`, `minimum_trades`,
`minimum_session_coverage_fraction`, `sample_interval_seconds`, and
`maximum_sample_gap_seconds`. There are no assumed thresholds or capital amounts.
Policy versions must increase sequentially. Sampling intervals are bounded to
30–300 seconds; the allowed gap is bounded to 30–600 seconds and cannot be shorter
than the sampling interval. Choose a gap that accommodates the configured worker
cadence. These bounds control resource usage, not recommended validation thresholds.

Each policy is audited with its effective timestamp and immutable strategy hash.
A replacement starts a new evidence period; existing sessions/trades are not
grandfathered. Publishing or reviewing evidence never grants trading permission.

## Observed coverage, not inferred days

The existing PAPER worker records opt-in, timestamped coverage for its supported
enabled reference strategy. Each sample includes policy identity, parameter hash,
worker-instance identity, session/calendar bounds, health/gate state and source
quote provenance. Fresh LIVE-origin depth is required during the session. A
healthy post-close, flat/reconciled worker observation is required to finish a day.

Coverage credits only intervals between healthy samples from the same worker
instance within the declared maximum gap and actual session bounds. Restarts,
outages and missing samples never fill in an interval. SYNTHETIC, REPLAY and
HISTORICAL observations cannot qualify as live-feed PAPER history. An eligible day
must meet the predeclared coverage fraction; merely having a trade on a date is
not enough. Coverage describes observed worker monitoring, not a guarantee of
continuous network delivery or continuous strategy signals. Ordinary risk gates
may legitimately prevent entries while monitoring continues.

## Costed, immutable trade evidence

Every newly closed PAPER trade receives a journal digest in the same transaction
as local fill/position/journal updates. Audit failure rolls those updates back;
broker-side simulated fills remain reconcilable rather than being resubmitted.
Recovery reconstructs one journal and verifies it independently.

Eligibility requires a costed, audited current PAPER journal, matching immutable
strategy parameters, LIVE-origin decision inputs available by entry, ordered
nonfuture timestamps after policy publication, and an eligible observed session.
Changed/unaudited/legacy journals are excluded explicitly, not retroactively
certified. Gross P&L, estimated charges and net P&L remain distinct. Descriptive
trade metrics use the same shared calculator as historical reports; no continuous
equity curve, Sharpe ratio or drawdown is invented from a list of closed trades.

## Authenticated API and review

- `POST /api/v1/strategies/{id}/evidence/policy`: strategy `version`, `policy`, `reason`.
- `GET /api/v1/strategies/{id}/evidence?version=...`: current server-derived evidence.
- `POST /api/v1/strategies/{id}/evidence/review`: strategy `version`, optional
  `backtest_id`/`walkforward_id`, `reason`, and confirmation
  `REVIEW STRATEGY EVIDENCE`.

Clients cannot submit session/trade counts or financial outcomes. Reviews verify
persisted report digests and specification hashes. Mixed-parameter walk-forward
aggregates cannot stand in for evidence about one fixed strategy. Future/undated
reports are refused. The review binds actor, policy, outcomes and report digests
to the audit trail and updates the registration's evidence summary atomically.

`live_approved` remains false, including when local numerical thresholds pass.
`EXTERNAL_DATA_VERIFICATION_REQUIRED` and `LIVE_ARMING_UNAVAILABLE` remain explicit.
LIVE enablement remains hard-blocked. The bounded reader refuses more than 100,000
coverage records or 10,000 journal records rather than truncating evidence.

The integration tests use isolated provider fixtures, including LIVE-origin-labelled
fixtures for exercising the eligibility rules. They are not real feeds, delivered
notifications, actual elapsed PAPER sessions, profitability, or M2 evidence.
