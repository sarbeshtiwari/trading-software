# PAPER news entry halts

This is conservative entry inhibition, not a trading signal or fact-verification
service. It does not submit orders, liquidate holdings, change risk limits, or
disable protective exits. PAPER remains the only implemented mutation mode.

## Owner policy

Authenticated `GET/PUT /api/v1/risk/news-policy` reads/versions the policy.
There is no enabled default or assumed confidence threshold. The owner supplies
`enabled`, `accept_uncalibrated_labels`, `minimum_confidence` (0–1), and
`max_age_seconds` (1–86400), together with the expected policy event ID and a
meaningful reason. Accepting model labels is mandatory when enabling the policy.
These labels are model self-reports, not calibrated probabilities or verified
financial facts. Disabling the policy does not release an existing halt.

The fixed reaction requires NEGATIVE direction, HIGH magnitude, the configured
confidence threshold, and current source-policy-admitted quotations. Every claim
must be within the admission's quoted support and the interpretation must name
exactly the held canonical instrument. Stale, future, unadmitted, unavailable,
wrong-origin and out-of-scope evidence cannot qualify through the admission path.
News service unavailability creates no new halt and never clears an existing one.

## Integrated lifecycle

Manual admission and automatic research production use the same admission/reaction
transaction. The PAPER worker also evaluates stored admissions against current
holdings after position monitoring, covering news received before a position opens.
Holdings come from actual persisted open positions and their originating proposals;
there are no synthetic holdings outside tests. The current execution path is CASH.

Each qualifying admission appends an instrument/origin-specific audit event and
notification outbox request in the same transaction. It archives policy, quotation
evidence, interpretation, admission and held-position IDs. The shared decision
pipeline records a named NEWS_HALT negative decision. Entry source checks,
transactional approval safety and final dispatch independently read the durable
halt. A caller's `news_halt=false` cannot override it. The halt is not a global
engine-error latch and does not inhibit another instrument or origin.

Exits do not consult this entry-only gate. The halt survives position closure,
process restart, source disablement and evidence ageing. Existing account slot,
reconciliation, tariff and other safety restrictions still apply independently.

## Review and release

`GET /api/v1/risk/news-halts` returns recorded halt states, including released ones.
The existing Risk page polls this API, displays evidence and provides an owner-only
review action. Failed reads show unavailable state rather than a healthy result.
Polling a changed halt head invalidates the typed review confirmation. Policy
edits retain the explicitly loaded audit version instead of silently adopting
another owner's intervening update; reloading clears the draft for a new review.

`POST /api/v1/risk/news-halts/{instrument_id}/release` requires origin, expected
head event ID, meaningful reason and `confirmation: "REVIEWED NEWS HALT"`.
Stale versions and unauthorized requests fail. Release appends an event; it does
not erase evidence, reset global latches or modify source policies. Previously
acknowledged admissions do not repeatedly notify/re-latch after review. Distinct
new admissions remain eligible for inhibition.

## Verification and limits

Integration tests use real admitted quotations, isolated external Claude HTTP
fixtures, the shared decision pipeline, PAPER fills/positions, durable audit/outbox,
restart, exits and authenticated APIs. A real Edge browser reads and releases the
recorded halt through the production React bundle. Fixture labels and execution
are not live market evidence, profitability or externally delivered notifications.

The actual reference worker is also tested ingesting a fixture, filling a position,
discovering previously admitted news on its next cycle and completing a costed exit
without clearing the halt. Storage fault injection proves admission, halt and
notification request roll back together; a retry commits each once. Tests cover
new admission versions invalidating old reviews and re-latching after release,
unadmitted/future/wrong-origin evidence and fail-closed detection of audit corruption.
The LIVE-tagged observation in the origin-isolation test is an isolated fixture,
not external market/news verification. Historical interpretation remains forbidden
by the existing no-lookahead guard; that guard is not weakened for a fixture.

NEWS-011 remains partial pending cross-process/PostgreSQL concurrency coverage and
derivative/underlying-family handling when that execution path is supported.
RISK-014 also remains partial: complete server-resolved ban/manual-block lifecycles
are separate pending work. No claims are made for Groww LIVE, deployed PostgreSQL,
external feed/Claude connectivity, remote notification delivery, M1 or M2.
Audit-chain protection retains the existing external-head/privileged-deletion
limitations; database permissions and backups remain operational obligations.
