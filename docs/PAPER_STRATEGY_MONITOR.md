# PAPER strategy degradation monitoring

This is an opt-in, deterministic stand-down control over actual closed PAPER
trades. It is not a strategy recommendation, profitability assessment, LIVE
approval, or replacement for portfolio daily-loss/drawdown limits.

## Owner configuration

Use the existing Strategies page, select **Monitored strategy**, and publish an
explicit versioned policy with an owner reason. The authenticated API equivalents:

- `GET /api/v1/strategies/{strategy_id}/degradation?version={strategy_version}`
- `POST /api/v1/strategies/{strategy_id}/degradation/policy`
- `POST /api/v1/strategies/{strategy_id}/degradation/reset`

Policy fields are `version` (next sequential policy version), `window_trades`
(1–1000), `minimum_trades` (not greater than the window),
`maximum_drawdown_amount` (positive currency amount), and `data_origin`.
The request also names the immutable strategy `version` and a reason.
No threshold, capital allocation or P&L is invented. Without a policy the state is
`UNCONFIGURED`, not healthy. Synthetic origins are for explicitly isolated tests;
the monitor does not generate market observations or trade history.

## What is measured

The window contains the latest closed trades for that exact PAPER strategy/version,
ordered by close time and ID. Future closes and future journal seals are excluded.
The monitor checks journal seals, revision status, strategy parameter hashes,
declared data origin, market availability, fees and `gross - charges = net`.
Unknown costs, mixed origins, corrupt evidence and unresolved journal corrections
make the whole selected window unavailable; they are not dropped to improve results.

For chronological net outcomes, cumulative P&L starts at zero. The running peak
starts at zero. Maximum closed-trade drawdown is the largest `peak - cumulative`.
For example, outcomes `100, -40, -80, 30` produce a maximum amount of `120`.
This does not assume an initial capital balance or measure intratrade/portfolio
mark-to-market drawdown. Expectancy and win fraction reuse shared descriptive
portfolio metrics. All charges remain sourced estimates, not certified broker bills.

Before the configured minimum count, state is `WARMING_UP`. At/above that count,
drawdown equal to or greater than the configured amount is `BREACHED`; otherwise
the evidence is `HEALTHY`. Policy changes retain existing window history.

## Enforcement and recovery

The actual worker evaluates after position/exit processing and before new strategy
evaluation. Execution preflight also checks independently. A configured enabled
strategy with `BREACHED` or `UNAVAILABLE` evidence is durably auto-disabled.
The registration change, audit evidence and notification-outbox request commit
together. Repeated checks do not duplicate the disable event or notice. A storage
failure aborts rather than reporting successful enforcement; normal worker and
execution fail-closed handling remains active. Delivery itself is separate and
unverified unless the notification service genuinely acknowledges it.

Existing positions retain normal monitoring and exit handling. This control blocks
new entries; it does not replace emergency flatten or portfolio risk controls.

An owner reset requires healthy evidence, a reason, the confirmation
`RESET STRATEGY DEGRADATION`, and the current policy/disable audit IDs. A policy
change alone never clears the latch. Reset leaves PAPER disabled: enabling is a
separate authenticated action. Normal enablement cannot bypass an auto-disable.
Unavailable or corrected economic evidence requires review; this implementation
does not silently repair or waive journal integrity. A reset does not grant LIVE
approval or weaken independent risk latches.

## Evidence and limits

Tests use real OMS fills, fee/FIFO accounting, journal seals and audit/outbox rows.
A production-worker fixture drives a losing reference trade through the complete
path and an actual browser verifies the disabled strategy and matching journal
drawdown on the Strategies page. Frontend tests cover guarded reset behavior.

STRAT-012 remains partial for broader SUPERVISED/LIVE integration and the broader
strategy-validation contract. This is a closed-trade PAPER monitor, not automatic
parameter optimization or learning. No external notification delivery, real market
performance or Groww LIVE execution is certified by these fixtures.
