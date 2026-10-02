# Closed-candle breakout v1

This is an unvalidated hypothesis, not a profitability claim. It is PAPER-only
in the current application service and is not automatically enabled at startup.

## Hypothesis and fixed rules

One-minute equity price continuation following a close strictly above the highs
of the preceding 20 candles, with that close above SMA20 and a fresh TRENDING_UP
regime. Input history must contain exactly 21 contiguous, closed, valid candles.
SMA20 and Wilder ATR14 use the existing technical-analysis implementation.

Entry reference is the latest close; the stop is two ATR below entry, rounded
down to the instrument tick; target is two times the resulting stop distance
above entry. Invalid/non-positive stops or unaligned entry prices stand down.
MIS product, long-only, one-hour maximum holding declaration, one-R trailing
declaration and close-below-SMA20 invalidation declaration use the existing exit
contract. Stop/target execution is integrated; runtime trailing/time/invalidation
supervision is still pending. The deterministic sizer remains authoritative.

The required signal confidence field is a **rule-completeness score** (1 only
when all entry predicates pass), not a win probability or calibrated predictive
confidence. It never changes risk limits or overrides sizing. No external text,
LLM output, fundamental value or sentiment is consumed by this strategy.

## Inputs, provenance and safety

`ReferenceIngestion` uses the existing `MarketDataProvider` methods. It validates
quote identity, freshness, provenance and depth; rejects future/forming candles,
gaps, malformed prices and unavailable history; persists candles and an immutable
hash-chained observation containing exact candles, quote and indicator values.
Raw provider text is excluded. Tests alone supply synthetic provider responses.

`ReferenceDecisionService` requires a complete session calendar, persisted fresh
regime, enabled registry version and available event-blackout evidence. It passes
the signal to the existing shared pipeline with a resolvable MARKET evidence
reference. The validator verifies the observation audit chain, instrument, price,
origin and knowledge timestamps. No order API is exposed by the strategy.

Entry context includes immutable, timezone-aware `as_of` metadata captured by
the decision service and passed by the engine after input validation. Both equity
and long-option reference signals use this instant, not another wall-clock read
after asynchronous work. The engine still rejects any different signal timestamp;
future/stale inputs are not tolerated to compensate for elapsed processing time.
Direct reference entry calls without decision-time metadata cannot produce a
valid signal. The moving-clock integration test exercises real worker ingestion,
sizing, risk, PAPER entry, protection, costed target exit, journal and API state;
its market observations and tariff are explicitly synthetic test fixtures.

Portfolio, costs, active risk limits and blackout context must come from trusted
application services. This service does not invent any of these inputs. The
worker now assembles fresh context from the PAPER account, active limits,
point-in-time persisted regimes/calendars and audited source inputs. Enabling
PAPER_WORKER_ENABLED alone is insufficient: register/enable the reference
strategy, supply valid inputs and pass every existing safety/readiness gate.
No credentials or missing source evidence is an unavailable state, not a demo.

## Verification and outstanding validation

Integration tests exercise provider -> ingestion -> indicators -> strategy ->
shared proposal validation -> sizing -> risk -> preflight -> PAPER order/fill ->
position -> protection check -> target exit -> gross P&L -> journal -> audit ->
authenticated workspace API. Disabled strategy and risk error latch create no
orders. Invalid provider observations are audited and not persisted as candles.

Costs/net P&L and FIFO, live ingestion verification, production regime/context
production, exhaustive negative paths, UI lifecycle rendering tests and durable
notification delivery are not certified by these tests. Worker scheduling and
restart deduplication have separate integration tests; input sources remain
unverified externally. Full vertical-slice completion, M1 and M2 are not claimed.

Keep parameters fixed for chronological out-of-sample and walk-forward tests.
Use point-in-time constituents and data versions, model fees/slippage, then
collect time-based PAPER evidence before considering supervised approval.
No backtest report, performance result or validation gate is claimed here.
