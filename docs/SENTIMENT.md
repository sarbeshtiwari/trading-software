# Sentiment evidence and limits

Sentiment is advisory context. It is not an order, a price forecast, a confidence
estimate invented by the application, or a standalone entry trigger.

News formula `CREDIBILITY_CONFIDENCE_RECENCY_V1` weights numeric interpretations
by configured source-tier weight times supplied confidence divided by
`1 + age_seconds / half_life_seconds`. It returns the complete evidence set,
weights, policy and exclusions. Scores/confidence are bounded; missing or
unverified evidence, contradictions, low confidence, stale and future knowledge
are excluded. A non-primary item requires two distinct source identities; source
independence still depends on the NEWS verification pipeline. Duplicate item ids
are rejected. No qualifying evidence produces an unavailable score.

Stored reads use the existing NewsItem table and exclude later receipt or
interpretation updates. That table does not retain interpretation versions:
an overwritten old interpretation is unavailable for an old query, never replaced
with the new value. NEWS verification and versioned interpretation history are
still pending, so SENT-001 is partial. Text bodies/titles never enter arithmetic
or entry-validation code and are not passed to an LLM by these components.

Market formula `EQUAL_COMPONENTS_V1` averages three contributions: index return
scaled/clipped to [-1,1], breadth `2*fraction-1`, and volatility relative to a
configured neutral level scaled/clipped to [-1,1]. Each sourced observation and
policy is retained; any missing/stale observation makes the composite unavailable.
Stored market-context assembly remains pending, so SENT-002 is partial.

Derivatives context reuses validated option-chain PCR and matched-time price/OI
build-up. PUT_HEAVY/CALL_HEAVY are descriptions, marked heuristic, not bullish or
bearish forecasts. VIX is retrieved through the existing MarketDataProvider,
with configured thresholds, provenance, wrong-index and stale/future checks.
Neither path is live-verified without an actual provider connection.

Entry-condition validation rejects sentiment/news-only paths, including an OR
branch or negated AND that could enter without non-sentiment evidence. It checks
structural dependencies, not the truth or profitability of the predicates. The
strategy registry now invokes it during validated specification registration.
