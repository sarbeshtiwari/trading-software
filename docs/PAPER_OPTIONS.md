# PAPER option lifecycle status

The existing OMS now supports a narrowly authorized single-long-option NSE/MIS
PAPER lifecycle. Futures, naked shorts, multi-leg structures and LIVE remain
unsupported. Service and real-browser tests pass; the complete backend regression
passes 1495 tests with three PostgreSQL-only skips. This is not external broker
verification.

The distinct `long-option-breakout` reference hypothesis is now registerable via
the authenticated API and existing Strategy page. Its provider-driven candidate
reaches deterministic risk approval and the PAPER OMS in integration tests. It
still requires independently checked contract policy, preflight and dispatch
authorization. Its fixed rules and missing performance validation
evidence are documented in `docs/strategies/LONG_OPTION_BREAKOUT/SPEC.md`.

- Shared sizing respects the greater of stop risk and declared maximum loss.
- Captured option constraints include type, expiry, strike, underlying and CE/PE.
  Missing metadata and expired contracts cannot produce broker fills.
- Typed long options reserve full premium, rounded upward to a paise, rather
  than using generic leveraged F&O margin estimates.
- FIFO partial fills/exits retain option identity and premium reservation across
  restart. Naked sell/reversal attempts fail before account mutation.
- Restore cross-checks option positions against persisted orders and fills;
  losing option identity cannot silently downgrade accounting.
- Explicit FNO/MIS tariffs require `OPTION_PREMIUM` charge basis. The audited
  cost store resolves catalog identity and refuses to apply them to futures or
  unknown contracts; broker fills independently verify tariff scope and captured
  option identity. All rates remain supplied, not inferred defaults.
- Costed partial fills and restart are tested: a synthetic 100-unit buy at 100
  followed by a sell at 110 has gross P&L 1000, estimated charges 28.62 and net
  P&L 971.38 under the explicitly synthetic tariff. This is broker accounting,
  separate from the OMS fixture below, not a claim about current Indian broker billing.

The deterministic broker fixture buys 50 at 100 and 50 at 101, then sells 75 at
110 and 25 at 110. Gross realized P&L is 725 after the first exit and 950 after
closure; remaining reserved premium is 2525 then zero. This fixture has no
option tariff and is not evidence of net profitability. Cash is the existing
simulation collateral balance; reserved premium reduces available funds, not
a second cash debit.

## Integrated evidence and authorization

The reference worker now obtains option chains through the existing market-data
provider interface for catalog options. It requires exact NSE underlying,
expiry, strike, side, symbol and data origin, plus complete finite timestamped
provider Greeks within the configured risk age limit. Missing/computed-without-
source/mismatched/stale/future evidence cannot stand in for provider observations.
Accepted chains use the existing snapshot store, and an audit observation retains
contract metadata, the chain and Greek values. Its evidence ID feeds the worker's
market context. Worker tests exercise collection and missing-Greek stand-down;
they intentionally do not bypass the remaining order gates. Shared decision and
entry-source/preflight guards now independently verify the persisted audit identity,
original timestamps, origin and current catalog metadata. Caller-supplied timestamps
cannot replace that evidence. Minimum DTE is checked against the published policy.
Preflight and final dispatch require the original audited contract observation and
the still-current owner policy. Replacing the policy invalidates pending entry
permission. Fees are mandatory; missing evidence never becomes a zero-cost fill.

The integrated synthetic fixture buys four units at 100, reserves premium and
planned fee risk, survives restart and exits at its stop of 96. Gross P&L is -16,
estimated charges 11.93 and net P&L -27.93. Actual OMS fills, protection, FIFO,
journal, audit, notification requests, API state and React rendering consume this
lifecycle. A separate case restarts a pending order before its fill. Late policy
revocation prevents broker submission; broker failure creates UNKNOWN state without
retrying the order or inventing a fill. Missing protection triggers emergency close.

These fixtures are not paper performance or real notification-delivery evidence.
Local computed Greek fallback, other derivative structures/products, exercise/
settlement, external feeds and full validation evidence remain outstanding.

## Published long-option policy

The existing reference-input API accepts `contract_source` with
`kind: "LONG_OPTION"`, an explicit `minimum_days_to_expiry`, and the same required
instrument/origin/source/known-at/validity/cost-reserve fields as the CASH policy.
The worker uses listed expiry metadata, persisted option evidence and a published
`OPTION_PREMIUM` tariff. It does not infer an expiry calendar or current tariff.
Insufficient DTE or missing evidence stands down. This publishes inputs, not order
permission by itself; independent execution checks remain mandatory.

Sizing floors long-option loss, margin and exposure at the tick-rounded entry
premium, even if the source quote was lower. Pure risk separately rejects an
understated declared bound or margin/exposure and rejects all naked option shorts.
There is no short-option enable flag or capped-structure execution implementation
yet. These restrictions must not be relaxed to produce a passing simulated trade.

Option risk decisions use formula `1.1.0`; historical `1.0.0` replay is audit-only.
Current evaluation never selects historical rules to authorize an order.

PAPER remains the default. Groww LIVE, external data and M1/M2 remain unverified.
