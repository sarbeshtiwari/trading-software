# IMPLEMENTATION_STATUS.md

**Project:** AI-Assisted Indian Equity & F&O Trading System (ATS)
**Last updated:** 2026-10-02
**Trading mode of record:** `PAPER` (no other mode can be reached — LIVE has no arming path yet)

> This file always reflects the REAL state of the project (execution contract §4).
> Nothing is marked complete unless it genuinely exists and runs.

---

## Current implementation focus

The application is in PAPER vertical-slice integration, not M1/M2 completion.
The latest verified checkpoint and exact next task are recorded at the end of
this file. The phase table below is the historical `f4f4825` component checkpoint;
its COMPLETE labels do not imply present end-to-end or external verification.

## Historical component checkpoint

```
PHASE 0 — REQUIREMENT SPECIFICATION     Status: COMPLETE (approved 2026-09-17)
P0      — FOUNDATIONS                   Status: COMPLETE
P1      — BROKER & DATA                 Status: COMPLETE
P2      — ANALYSIS                      Status: IN PROGRESS
          technical analysis            COMPLETE
          options Greeks                COMPLETE
          option chain                  COMPLETE (component tests)
          option selection / payoff     COMPLETE (component tests)
          futures analysis              COMPLETE (component tests)
          regime classification/history IMPLEMENTED; strategy/journal wiring pending
          equity analytics              TESTED; daily scheduling/context wiring pending
          sentiment                     TESTED; source/market assembly pending
          fundamentals                  COMPLETE (component/API/context tests)
```

### Historical test results at f4f4825

```
776 passed, 3 skipped, 0 failed          (backend/, pytest)
skipped: 3 PostgreSQL-only schema tests (need ATS_TEST_POSTGRES_URL)
```

---

## Requirement status roll-up

| Status | Count | Meaning |
|---|---|---|
| `[✓]` Tested | 229 | Requirement-specific evidence; not blanket end-to-end or external certification |
| `[x]` Implemented | 10 | Code exists; some requirement-specific acceptance remains unverified |
| `[~]` In progress | 133 | Partial implementation/integration; limitations recorded below |
| `[ ]` Not started | 161 | |
| **Total** | **533** | |

---

## P1 — Broker & data: what was built

### Groww adapter (`backend/app/brokers/groww/`)
Built against the documented contract and exercised through the **real client
code** using an httpx mock transport over 23 hand-built fixtures.

* **Client** — base URL, the three required headers, envelope unwrapping, timeout
  and connection mapping, and a *bounded* single re-authentication on `GA005`
  (an unbounded refresh loop would exhaust the 150-per-day token budget in
  seconds and lock the account out of authentication for the rest of the day).
* **Error taxonomy** — `GA000`…`GA007` mapped to typed exceptions. `GA007`
  (duplicate reference) is modelled as an idempotency *signal*, never retried.
* **Rate limiter** — sliding windows for both published windows per category.
* **Retry policy** — transient only, full-jitter backoff, and a hard rule that
  order-creating calls execute exactly once.
* **Auth** — both documented flows, TOTP preferred (its token does not expire),
  token cache, and a local guard on the 150/24h token-endpoint cap.
* **Endpoints** — orders (create/modify/cancel/detail/by-reference/list/trades),
  positions, holdings, margin, quotes with 50-symbol batching, historical candles
  with per-interval window splitting, option chain and Greeks.
* **Endpoint registry** — every path marked `documented` or `inferred`, with a
  warning the first time an inferred path is used, and all of them enumerated in
  `docs/LIMITATIONS.md`.
* **Websocket feed** — subscription budget with priority eviction, reconnection
  with backoff, prior subscriptions restored, and the outage published as a
  `FEED_GAP` event so consumers know their data has a hole.

### Paper broker (`backend/app/brokers/paper/`)
A full `BrokerProvider` that mirrors Groww's constraints exactly, so a strategy
cannot come to depend on something the real broker would refuse. Simulated
account with labelled-estimate margin, fills walked against the live depth book,
duplicate-reference idempotency, and state that survives a restart.

### Market data (`backend/app/marketdata/`)
Live provider, historical provider, replay provider; quote cache; staleness
policy; data-quality validation; session-anchored tick-to-bar aggregation;
interest-based subscription management; calendar-aware gap detection and targeted
backfill; warm-up enforcement; and an archive coverage report that says out loud
when a backtest window is too short to mean anything.

### Instruments (`backend/app/instruments/`)
CSV master loader with resolved (not assumed) headers, upsert semantics,
deactivation rather than deletion, and an in-memory index for lot/tick/expiry/
strike lookups.

### Calendar and sessions (`backend/app/core/`)
Trading calendar and the five session phases, asserted at every boundary minute.
The shipped holiday file is deliberately partial and reports its own
incompleteness through the market-status health check.

### Health checks
Eleven of the thirteen §10 checks now exist. `news` and `order_service` remain,
and the health endpoint lists them as missing rather than implying they pass.

### Four real defects the tests caught during P1
1. The token-bucket rate limiter allowed **400 orders through a 250/minute cap**
   (capacity plus continuous refill). Replaced with sliding windows.
2. The baseline migration synced *current* metadata, so it would have collided
   with every table added later. Pinned to an explicit baseline list.
3. `lot_size or 1` in the instrument loader silently turned an explicit lot size
   of `0` into `1` — sizing an F&O order as a single unit instead of a lot.
4. The instruments health check trusted a cached index, so it would report PASS
   after the instrument table had been emptied. It now re-reads every time.

---

## P2 — Analysis: progress so far

### Technical analysis (`backend/app/analysis/technical/`) — COMPLETE

Framework plus 26 registered indicators, every one tested against a hand-computed
reference value rather than against whatever the code happened to return.

* **Framework** — results align 1:1 with input bars, `None` during warm-up (never
  a repeated seed value), and a declared minimum lookback that raises rather than
  returning a plausible number from too little data.
* **Wilder smoothing is distinct from EMA.** RSI, ATR and ADX use `1/period`;
  substituting `2/(period+1)` shifts every reading and is the classic source of
  an indicator disagreeing with the chart it is compared against.
* **True range accounts for gaps**, so ATR-derived stops are not systematically
  too tight.
* **Donchian excludes the current bar** — a breakout system comparing price to a
  channel that already contains its own bar can never trigger.
* **Multi-timeframe alignment is strict**: a higher-timeframe value becomes
  visible only once its bar has closed. Asserted directly by
  `test_mtf_no_lookahead`.
* The snapshot builder records the parameters and the bar timestamp alongside the
  values, so an audit record can be replayed rather than merely read.

### Options Greeks (`backend/app/fno/greeks/`) — COMPLETE

Black-Scholes, analytic Greeks, IV solver, assumptions, time conventions, source
resolution and portfolio aggregation — validated against the standard textbook
reference case to 1e-5.

* **Conventions are stated and tested**: theta per calendar day, vega per
  volatility point, rho per rate point. Vendors differ, and a silent mismatch
  hedges a book in the wrong direction.
* **IV non-convergence returns `None` with a reason**, never a number. A
  fabricated IV poisons vega, the skew curve and every options decision after it.
* **Expiry is 15:30 IST**, and both time conventions (calendar and trading
  session) are offered explicitly rather than one being hidden inside a helper.
* **Short options flip Greek signs**: a short straddle is short gamma and *long*
  theta, and the aggregator reports it that way.
* Positions whose Greeks are unavailable are **listed, not skipped** — a
  portfolio delta computed from half the book looks like a complete answer.

### Two more real defects caught by tests

5. ADX returned `None` in a perfectly flat market, because DX was 0/0. A
   zero-movement series now reads zero trend strength instead of no value at all.
6. The IV solver rejected valid quotes for deep in-the-money European puts by
   comparing against **undiscounted** intrinsic value. The no-arbitrage floor for
   a European option is the discounted bound; such a put legitimately trades
   below intrinsic because it cannot be exercised early.

---

## Unverified on this machine (not a claim of completeness)

Full detail in `docs/LIMITATIONS.md`. In short:

| Item | Why |
|---|---|
| Every live Groww call | No credentials (dependency D1) |
| Inferred endpoint paths and the REST token body | Not published by the vendor |
| Websocket message shapes | Not published; normalisation is a best reading |
| Docker Compose stack | Docker is not installed on this host |
| TimescaleDB hypertables, append-only triggers, fill-sum trigger | No PostgreSQL server here |
| Paper P&L is **gross** | The Indian cost model is P5 work; the paper broker warns on every connect |

---

## Continuation verification — 2026-09-20

- Resumed commit `efa06e5`; baseline reproduced: 389 passed, 3 skipped.
- The existing chain DTO and snapshot table were reused, not replaced.
- README and the phase ledger lagged the actual P2 checkpoint; corrected.
- OC-001…OC-008 now have 50 additional passing tests, including Groww fixture
  parsing, hand-computed analytics, malformed/missing inputs, expiry, freshness,
  schema validation, persistence, immutable retries and receipt-time no-lookahead.
- Full suite: **439 passed, 3 skipped**, with two pre-existing dependency warnings.
  Focused Ruff checks pass. PostgreSQL-only checks still skip; Docker executable
  is unavailable. Groww live connectivity/execution remains unverified.
- Tests caught a tuple/list snapshot round-trip mismatch (fixed). Inspection also
  found fractional OI truncation and skipped malformed strikes in the Groww
  parser; both now fail explicitly with regression tests.
- Snapshot capture has configurable per-expiry cadence and durable history.
  No autonomous collector is running yet; the future engine loop must invoke
  capture. Typed chain context is ready for subsequent strategy/AI consumers;
  no strategy or AI prompt integration is claimed at this stage.
- Mathematical conventions and limitations: `docs/OPTION_CHAIN.md`.
- User-owned untracked `prev_chat.txt` was not modified or staged.
- OC checkpoint committed locally as `6d85823`.
- Following options unit completed; see the next checkpoint section.
  Existing resolver already offers basic expiry/strike lookups and the Greeks
  module has intrinsic/time-value helpers; inspect and reuse these.

## Options checkpoint — 2026-09-20

- OPT-001…OPT-005 implemented and verified with 30 new deterministic tests:
  instrument validation, moneyness, strike and expiry selection, liquidity,
  signed lot payoff, exact extrema/breakevens and named finite-loss structures.
- Full suite: **469 passed, 3 skipped**; focused Ruff checks pass.
- OPT-008 is **in progress**: selection enforces liquidity, but execution-time
  enforcement awaits the order pipeline. OPT-006 risk enforcement remains pending.
- Unknown weekly/monthly metadata remains unavailable; no exchange weekday is
  assumed. The existing loader does not populate this metadata. Current instrument
  resolution is not a historical master archive. See `docs/OPTIONS_ANALYSIS.md`.
- Covered-call analysis requires share coverage in its input, but does not prove
  actual account holdings. No order integration, naked-short enablement or
  live Groww verification is claimed.
- Options checkpoint committed locally as `e8f08db`.
- Futures unit completed next; see the following checkpoint.

## Futures checkpoint — 2026-09-20

- FUT-001…FUT-003 implemented: validated near/next/far resolution, expiry cutoff,
  simple ACT/365 basis/carry, calendar spread and advisory rollover plans.
- 20 new deterministic tests; full suite **489 passed, 3 skipped**. Focused Ruff
  checks pass. PostgreSQL-only checks and Groww live verification remain unavailable.
- Plans validate lot changes, tick sizes, volume/spread, quote freshness and
  synchronisation. Long and short signs are tested separately. Missing fees
  produce an unavailable total cost; supplied estimates are explicitly labelled.
- No new defects were observed in existing modules in this batch.
- See `docs/FUTURES_ANALYSIS.md`: no execution, margin verification or historical
  instrument-master archive is claimed. Existing risk defaults remain unchanged.
- Futures checkpoint committed locally as `1059456`.
- Regime components completed next; see the following checkpoint.

## Regime checkpoint — 2026-09-20

- REG-001…REG-003 implemented and tested: bounded sourced inputs, reused
  ADX/SMA/realised-volatility functions, deterministic labels, confirmation
  hysteresis, normal-transition cap, and immediate safety overrides.
- REG-005 **in progress**: durable history, restart continuity, immutable API
  semantics, provenance and receipt-time no-lookahead are tested. Returning a
  regime record id does not complete journal attribution; that wiring is pending.
- REG-006 **in progress**: sourced point-in-time event windows engage EVENT_RISK.
  Affected-strategy suppression requires REG-004/STRAT-002, not yet implemented.
- 19 added tests; full suite **508 passed, 3 skipped**, two existing dependency
  warnings. Focused Ruff checks pass. SQLite migration upgrade/downgrade/upgrade
  and full metadata equivalence pass. PostgreSQL migration execution is unverified.
- Migration 0003 adds only regime_history. Existing journal schema is unchanged;
  no journal integration is claimed. The initial migration uses current metadata
  for baseline tables: future changes to existing columns must account for that
  when adding incremental migrations. This batch avoids modifying baseline columns.
- Classifier policy and inputs are persisted. Missing/stale data means UNKNOWN,
  not a normal regime. Real event, breadth and IV data must be supplied explicitly.
- See `docs/REGIME_ANALYSIS.md` for conventions, policies and remaining integrations.
- Exact next unit: **EQ-001…EQ-007**. Inspected requirements include daily universe
  resolution with exclusions, position-aware liquidity, relative strength, sector
  mapping, ATR/realised-volatility/beta, pre-open gap ranking, and event blackouts.
  Reuse the existing instrument master, technical functions and new EventCalendar.
  Then SENT-001…SENT-005, FUND-001…FUND-008, and P3 decision core.
- Preserve pending integrations: REG-004, REG-005 journal attribution, REG-006
  strategy suppression, OPT-008 pre-order liquidity, chain capture engine wiring.
  P2 and the overall M1 contract remain incomplete; M2 has no validation evidence.

## Equity checkpoint — 2026-09-20

- EQ-002, EQ-003, EQ-004 and EQ-006 implemented and tested: position-aware
  actual-turnover/spread screening, aligned relative-strength ranking, sourced
  sector mappings with explicit unmapped symbols, and pre-open/official-open gaps.
- EQ-001 remains in progress: deterministic daily-session universe resolution
  from stored evidence works; automatic daily engine scheduling is not connected.
- EQ-005 remains in progress: ATR%, annualised realised volatility and beta pass
  independent reference tests; automatic daily refresh remains pending.
- EQ-007 remains in progress: typed blackout context and unavailable-calendar
  handling work; risk/AI consumer wiring depends on later phases.
- New equity evidence snapshots preserve membership/sector revisions and source
  timestamps. Receipt-time filtering prevents late ingestion influencing history.
  Turnover is never approximated silently from close * volume.
- 18 new tests; full suite **526 passed, 3 skipped, 0 failed**. Focused Ruff and
  SQLite migration upgrade/downgrade/schema-equivalence checks pass.
- Migration 0004 adds equity_evidence without changing baseline tables.
- Documentation: `docs/EQUITY_ANALYSIS.md`. PAPER remains default. No live Groww,
  external constituent feed, automatic collector or profitability is claimed.
- Exact next unit: **SENT-001…SENT-005**, then **FUND-001…FUND-008** and P3.
  Preserve EQ-001/EQ-005 daily wiring and EQ-007 context wiring for the engine.

## Sentiment checkpoint — 2026-09-20

- EQ checkpoint committed as `51f88b2`.
- SENT-003 and SENT-004 implemented/tested: descriptive OI/PCR context and VIX
  retrieval/classification through MarketDataProvider, with time/provenance checks.
- SENT-001 remains partial: confidence/credibility/recency aggregation and stored
  NewsItem reads work; NEWS verification and immutable interpretation versions
  remain pending. Later-updated legacy items are excluded, not leaked backwards.
- SENT-002 remains partial: sourced composite formula and missing/stale exclusions
  work; stored market-context assembly is not yet wired.
- SENT-005 remains partial: structural validation rejects sentiment/news-only
  entry paths, including OR and NOT branches; registry enforcement awaits P3.
- 16 added tests; full suite **542 passed, 3 skipped, 0 failed**; focused Ruff passes.
- External text is never an instruction: raw titles/bodies do not enter numeric
  aggregation or entry validation. No confidence is fabricated.
- See `docs/SENTIMENT.md`. Exact next unit: **FUND-001…FUND-008**, then P3.
- Existing fundamentals table is date-only and cannot preserve same-day revisions.
  A separate version ledger is needed without rewriting that baseline table.

## Fundamentals checkpoint — 2026-09-20

- Sentiment checkpoint committed as `bc2b21f`.
- FUND-001…FUND-006 and FUND-008 implemented/tested: manual CSV/JSON source,
  same-day revisions, per-metric freshness, valuation API, growth/profitability,
  balance health, stored versioned scores and corporate calendars feeding blackouts.
- FUND-007 awaits strategy-context enforcement in P3; it is not marked complete.
- 18 added tests; full suite **560 passed, 3 skipped, 0 failed**. Focused Ruff and
  SQLite migration/schema-equivalence tests pass; PostgreSQL runtime is unverified.
- Migration 0005 adds version ledgers alongside the unchanged legacy tables.
  Conflicting same-time revisions fail. Known-time plus receipt-time reads prevent
  look-ahead, including late imports and revised/cancelled event calendars.
- Scoring heuristics and units are explicit in `docs/FUNDAMENTALS.md`; no vendor
  data, account capital, missing metric or result was invented.
- Exact next coherent unit: **P3 strategy contract/registry/context/signal gates**
  (STRAT-001…004, REG-004, FUND-007, SENT-005), then sizing and deterministic risk.
- P2 remains incomplete at integration level: daily equity scheduling, market
  sentiment assembly, NEWS verification/history, chain collection, journal and
  risk/AI context wiring remain tracked. Proceed into P3 to satisfy dependencies,
  without claiming P2 or M1 completion.

## Strategy gate checkpoint — 2026-09-20

- Fundamentals checkpoint committed as `a3fdf08`.
- STRAT-001/003/004/009, REG-004/006, FUND-007 and SENT-005 implemented/tested:
  mandatory validated declarations, typed stop-bearing signals, declared-only
  context, regime/event/LLM gates and registry sentiment-only rejection.
- Persisted per-mode enablement and read-only API work; STRAT-002 remains partial
  until the order pipeline consumes the registry. LIVE remains hard-blocked.
- STRAT-007 remains partial: deterministic fixture evaluation is tested, but no
  production reference strategy exists. No backtest/profitability claim is made.
- 34 added tests; full suite **594 passed, 3 skipped, 0 failed**. Focused Ruff passes.
- Closed-candle boundaries, future/stale inputs, provenance, signal identity,
  changed-version rejection, missing calendars and opt-in fundamentals tested.
- Exit declarations are not exit implementations; approval, arbitration,
  performance monitoring and hash-chained audit remain pending.
- See `docs/STRATEGIES.md`. P2 scheduling/journal/risk/AI integrations remain open.

## Sizing checkpoint — 2026-09-20

- Strategy gate checkpoint committed as `af9bdc3`.
- SIZE-001…009 implemented/tested as independent sizing components: configured
  risk and daily budgets, downward lots, conservative ticks, margin/exposure/
  concentration caps, optional ATR/Kelly caps and explicit zero-size outcomes.
- Owner settings provide approved defaults. Missing capital is an explicit error;
  requested risk cannot raise the owner's per-trade limit. No LLM quantity input.
- Full-precision inputs/policy/result persisted per proposal; replay detects
  mismatched quantity and unsupported formula versions. No schema change needed.
- 27 tests added, including a deterministic 256-case cap-invariant matrix.
  Full suite **621 passed, 3 skipped, 0 failed**; focused Ruff passes.
- Environment defect: Hypothesis generation raised an internal TreeNode
  AttributeError before evaluating the property. The invariant was instead
  verified exhaustively over a deterministic boundary matrix; dependency repair
  is not silently claimed.
- Integration limits: account reservations, broker margin refresh, full risk veto
  and order preflight remain pending. Sizing alone never authorizes execution.
  See `docs/SIZING.md`; stop-based planned risk is not a gap-loss guarantee.

## Risk core checkpoint — 2026-09-20

- Sizing checkpoint committed as `62ad36e`.
- RISK-001…004, 007…015 and 019 implemented/tested at component level:
  pure deterministic engine, individually testable rule registry, numerical
  boundaries, provenance/freshness, price/lot validity, margin/depth, named
  instrument blocks and full decision persistence/replay.
- RISK-005/006 remain partial: daily-loss and drawdown vetoes plus consumed latch
  state work; durable session disarm, critical alerts and explicit re-arm do not.
- RISK-016 remains partial: immutable validated limits exist, not an authenticated
  audited activation path. RISK-017 rejects exceptions, but runtime disablement/
  critical alerts and no-order integration still await orchestration.
- 31 tests added; full suite **652 passed, 3 skipped, 0 failed**; focused Ruff passes.
- Replay tests exposed Decimal/string type drift in polymorphic numeric audit
  fields. Canonical Decimal strings now make stored rule evidence reproducible.
- Entry evaluation is not order permission. Live arming, broker verification,
  derivative aggregate risk, capital reservations and latest-state preflight
  remain required. See `docs/RISK.md`.

## Proposal validation checkpoint — 2026-09-20

- Risk core checkpoint committed as `3985cfe`.
- AID-001/002/003/005/010 implemented/tested: strict owner-field schema, specific
  semantic rejection codes, current instrument eligibility, tick/lot/side checks,
  entry/ATR sanity bounds and point-in-time database evidence resolution.
- Control-field tampering is rejected and logged without exposing raw payloads.
  External news/title/thesis text remains data, not instructions.
- Equity references enforce instrument/origin/freshness; fundamentals enforce
  receipt/known times; news must be verified, mapped, nonconflicting and not
  updated after the decision. Citation resolution is not full claim grounding.
- AID-006 remains partial: low-confidence proposals are rejected/logged, but not
  yet written to the negative-decision ledger. AID-007 has a passing transitive
  import-graph test; actual shared pipeline handoff to risk is still pending.
- 28 tests added; full suite **680 passed, 3 skipped, 0 failed**; focused Ruff passes.
- SQLite test fixtures now explicitly store UTC, matching production ledgers;
  timezone stripping otherwise caused false unresolvable-evidence failures.
- Quantity override/execution and quantitative-vs-LLM pipeline parity are not
  claimed. See `docs/PROPOSALS.md`.

## Shared decision pipeline checkpoint — 2026-09-20

- Continued from `5d2dc45`; baseline independently reproduced: 680 passed,
  3 skipped, 0 failed.
- AID-006/007/008/009 implemented/tested: quantitative and advisory JSON paths
  share validation, independent sizing and risk. Low-confidence/negative decisions
  are persisted with cycle, stage, reason and trusted state.
- AID-004 remains partial: candidate/risk quantity overrides advisory quantity,
  but no execution-boundary verification exists yet.
- All proposal/sizing/risk/candidate writes share one transaction; injected write
  failure rolls back and cannot return approval. Existing replay services verify
  the new records. Regime, identity, mode, freshness and strategy gates remain on.
- Full suite: **687 passed, 3 skipped, 0 failed**; focused Ruff passes.
- Contract margin/notional and owner limits are trusted controller inputs, not LLM
  fields. No runtime scheduler, capital reservation or broker submission is added.
- See `docs/DECISION_PIPELINE.md`. PAPER-only pipeline; M1/M2 incomplete.

## Exit/arbitration checkpoint — 2026-09-20

- Shared pipeline committed as `4f09ca4`.
- STRAT-006/008 implemented/tested: mandatory executable exit policy, shared
  stop/target/trailing/time/invalidation evaluator, priority-based arbitration.
- Registry refuses replaced exit methods. Trailing never weakens initial stops.
  Future/stale marks and mismatched invalidation inputs are rejected.
- Old strategy specifications without exit_policy fail closed and need explicit
  new versions. No old approval is transferred.
- 14 tests added; full suite **701 passed, 3 skipped, 0 failed**. Focused exit/
  registry rerun: 26 passed, 0 failed, no warnings after correcting a test-only
  Decimal serialization warning. Focused Ruff passes.
- Arbitration logs both winners and suppressed signals; input order and AI
  confidence cannot change configured priorities. No exits/orders are fabricated.
- See `docs/EXITS_AND_ARBITRATION.md`; monitoring/OMS integration is still pending.

## Audit checkpoint — 2026-09-20

- Exit/arbitration checkpoint committed as `6193b11`.
- AUDIT-002/004/007 implemented/tested: canonical hash-chain writer/verifier,
  copied decision/source/regime snapshots, redaction, and atomic positive/
  negative pipeline audit records. Original sources cannot rewrite past evidence.
- AUDIT-001 remains partial until real PAPER e2e order/fill/exit/result events
  exist. Missing events are not fabricated. PostgreSQL append-only triggers and
  concurrent runtime behavior remain unverified without PostgreSQL.
- 7 tests added; full suite **708 passed, 3 skipped, 0 failed**; focused audit/
  proposal/pipeline tests: 42 passed. Focused Ruff passes.
- Found a portability defect: rejection codes can exceed AuditEvent.risk_verdict's
  32-character column. Store APPROVED/REJECTED there, and full reason codes in
  structured result metadata. Text bounds now match PostgreSQL even on SQLite.
- Integrity checks bind all columns, detect mutations/gaps and support external
  expected-head/count anchors. Tail deletion without an external anchor cannot
  be detected; no cryptographic integrity guarantee beyond this is claimed.
- Updated stale proposal/strategy/sentiment documentation to match verified work.
  See `docs/AUDIT.md`.

## Durable risk safety checkpoint — 2026-09-20

- Continued from `448aea3`; baseline independently rerun: 708 passed, 3 skipped.
- Added audited daily-loss, sticky drawdown and engine-error latches, reconstructed
  from persisted audit chains on every decision and after restart.
- Daily loss remains latched through rebound; only a fresh later IST date releases
  that latch. Drawdown/errors have no unauthenticated reset path.
- Entry enforcement runs before sizing and again before approval persistence.
  Unrelated gate blockers are preserved; risk-only blockers permit exits.
- CRITICAL local logs/audit events are real; remote delivery is not claimed.
  Storage/integrity failures fail closed; failed writes cannot be claimed durable.
- Optimistic audit sequence guards prevent stale writers from overwriting latches.
  A separate-process SQLite test proves committed state survives process restart.
- 22 new tests; full suite **730 passed, 3 skipped, 0 failed**, two existing
  dependency warnings. Focused Ruff and git diff checks pass.
- RISK-005/006/017 stay partial: authenticated manual re-arm, monitoring/OMS,
  broker-boundary enforcement and remote notification integration remain pending.
  No new requirement is promoted merely because a component exists.
- See `docs/RISK_SAFETY.md` for concurrency, retention, failure and scope limits.

## Notification routing checkpoint — 2026-09-20

- Durable risk checkpoint committed locally as `2ac30fb`.
- NOTIF-002 policy implemented: explicit per-severity channel routing, validated
  IST quiet windows, midnight wrapping and CRITICAL bypass. No delivery inferred.
- 16 focused tests passed, 0 failed/skipped; focused Ruff passes. Latest full
  suite remains 730 passed / 3 skipped before this independent routing unit.
- NOTIF-002 remains partial until policy is wired to actual notification delivery.

## Notification delivery checkpoint — 2026-09-20

- Routing checkpoint committed locally as `409ba4f`.
- Added real Telegram HTTPS/SMTP STARTTLS transports, configured channel selection,
  bounded background enqueue/retries/timeouts, redacted payloads and explicit
  provider-acknowledged/failed/disabled/cancelled outcomes. Missing configuration
  disables channels with warnings. No network fixture exists outside tests.
- NOTIF-002/007 now tested through the delivery service; NOTIF-001 stays partial
  because actual remote sends are unverified and runtime lifecycle wiring remains.
- Full suite: **760 passed, 3 skipped, 0 failed**. Added two further transport
  safety tests; focused notification rerun: **32 passed, 0 failed/skipped**.
  Focused Ruff passes; only existing dependency warnings in the full suite.
- Delivery is at-least-attempted, not exactly-once or proof of recipient reading.
  Queue/outcomes are not durable; SMTP cancellation cannot stop an active thread.
  See `docs/NOTIFICATIONS.md` for setup and limitations.

## Notification suppression checkpoint — 2026-09-20

- Delivery checkpoint committed locally as `fab569a`.
- NOTIF-006 implemented/tested: bounded condition suppression, per-severity
  admission limits, explicit suppression counters, monotonic expiry and no
  admission consumption when enqueue fails. Severity escalation has its own budget.
- 100 repeated conditions produce one queued delivery plus 99 counted suppressions.
  Active condition keys are not evicted to admit new messages; state is process-local.
- Full suite **768 passed, 3 skipped, 0 failed**; notification tests **38 passed**.
  New notification modules pass Ruff. Existing conftest.py import-style lint
  findings remain unrelated; only environment isolation prefixes changed there.
- Test environment now explicitly clears SMTP/Telegram/notification variables so
  developer credentials cannot leak into isolated notification tests.

## Risk/notification runtime checkpoint — 2026-09-20

- Suppression checkpoint committed locally as `af662b6`.
- FastAPI now starts/stops optional configured delivery. Remote notifications
  default disabled; `.env.example` documents opt-in, routing and bounded limits.
- Committed daily-loss/drawdown/error transitions enqueue CRITICAL alerts linked
  to their real audit event IDs. Persistence failures emit separate degradation
  events, never pretend a breach audit was committed. Transport failures do not
  alter entry gates. Real remote delivery remains unverified.
- Startup reconstructs all data-origin safety chains for the configured mode
  before readiness. Risk-only blockers preserve exits and other control sources.
- Found/fixed readiness regression: restoration failure must not skip dependency
  check registration; readiness/startup results must include existing gate blocks,
  rather than report trading allowed from healthy dependencies alone.
- 8 integration tests added. Final full suite **776 passed, 3 skipped, 0 failed**;
  two existing dependency warnings. New notification/safety modules pass Ruff.
  Pre-existing config/main/startup import/style lint findings were not refactored.
- NOTIF-003 is partial: risk producers are wired, other mandatory producers and
  durable delivery outcomes/outbox are pending. RISK-005/006/017 remain partial,
  especially authenticated re-arm, actual monitoring/OMS and broker dispatch.

## Superseded continuation task (checkpoint f4f4825)

Implement **authenticated risk-control prerequisites** next:
1. AUTH-007/SEC-002/SEC-005: Argon2id owner password verification, expiring JWT
   access/rotating refresh sessions, durable logout/revocation and brute-force
   lockout. Settings already declare password hash/JWT secret; dependencies are
   installed. Missing credentials must disable login, not invent an owner secret.
2. Protect API routes by default and add an unauthenticated route-sweep test.
   Define only necessary authentication bootstrap exceptions explicitly. Existing
   tests must authenticate or use explicit test dependency overrides, not weaken
   production auth. Preserve local-only defaults and secret redaction.
3. Then RISK-006/016 and AUDIT-006: authenticated, transactionally audited manual
   re-arm/config activation with before/after, reason, verified actor and fresh
   trusted account state. No actor-string-only reset or client-invented portfolio.
   Do not clear session daily loss or unrelated startup/reconciliation/kill gates.
4. Complete notification outbox/durable outcomes and remaining mandatory producers
   alongside P4 execution/preflight/OMS. Recheck risk latches at actual dispatch;
   advisory approval is not order authorization. Remote delivery/Groww validation
   need real authorized credentials; do not fabricate proof.

The safety ledger uses existing AuditEvent tables (no new migration yet). New
auth storage should use a new migration after 0005; avoid mutating baseline 0001
metadata-bound table schemas. Preserve the optimistic audit sequence guard.

Current components are callable and tested, not a running autonomous trading
loop. Scheduler, capital reservations, live-state adapters, reference strategy
validation, P2 integration gaps and later phases remain pending. M1/M2 are not
complete. PAPER remains default; Groww live execution is unverified. Preserve
user-owned `prev_chat.txt`. Local commits only; never push.

## Phase ledger

| Phase | Name | Status | Completed |
|---|---|---|---|
| Phase 0 | Requirement specification | COMPLETE | 2026-09-17 |
| P0 | Foundations | COMPLETE | 2026-09-17 |
| P1 | Broker & data | COMPLETE | 2026-09-18 |
| P2 | Analysis | IN PROGRESS | — |
| P3 | Decision core | IN PROGRESS | — |
| P4 | Execution | IN PROGRESS | — |
| P5 | PAPER mode complete | NOT STARTED | — |
| P6 | AI layer | NOT STARTED | — |
| P7 | Validation (backtest / walk-forward) | NOT STARTED | — |
| P8 | Dashboard | IN PROGRESS | — |
| P9 | Supervised & Live | NOT STARTED | — |
| P10 | Learning & reporting | NOT STARTED | — |
| P11 | Completion audit | NOT STARTED | — |

## Current checkpoint: authenticated application workspace (2026-09-20)

The owner's vertical-slice priority supersedes the old sequential requirement
plan: safety -> PAPER OMS/fills/positions/exits -> orchestration -> connected UI
-> backtesting -> remaining production layers. No remote push is permitted.

- Verified f4f4825 baseline: 776 passed, 0 failed, 3 skipped. Inspection confirmed
  there was no frontend or complete OMS/intraday loop. Existing component evidence
  must not be read as application end-to-end proof.
- AUTH-007, SEC-002, SEC-005 now integrated: Argon2id login, expiring JWT,
  database-backed rotating refresh/revocation, durable lockout, audited events,
  fail-closed route authentication. Passwords/tokens are not echoed in errors.
- Authenticated PAPER risk configuration activation and manual re-arm are wired
  to actual API routes with verified actor and atomic audit/config records.
  Pipeline approvals recheck active configuration. Re-arm requires fresh server
  account evidence and reconciliation, preserves daily-loss/unrelated gates.
  RISK-005/006/016/017 remain partial: actual dispatch/monitoring integration and
  process/agent control isolation must still be verified, not inferred.
- Nine initial React views use the actual authenticated workspace/risk APIs.
  Typed workspace client derives from exported OpenAPI. Empty/degraded states are
  explicit. This is read/control integration, NOT an executed trade demo. Full
  market charts, exits/actions, journal, performance and live utilisation pending.
- PAPER-007 downgraded to partial: prior component tests did not demonstrate
  labelling across the nonexistent UI/reports. Frontend requirements remain partial.
- Final backend: **784 passed, 0 failed, 3 skipped** (PostgreSQL external fixtures
  unavailable), two existing dependency warnings. Frontend: **3 passed, 0 failed**;
  TypeScript/production build pass; npm install audit reports **0 vulnerabilities**.
  Focused new backend Ruff and git diff whitespace checks pass.
- Defects caught/fixed: auth epoch floats violated model invariants (now integer
  epochs); route sweep adapted to actual OpenAPI routes; integer singleton lookup
  corrected to the real system-state key; npm optional-peer resolution and a
  vulnerable test-runner release resolved with pinned patched tooling.
- Ledger: 204 marked tested, 13 implemented/unverified, 37 partial, 279 not started.
  The 204 include historical component-only claims awaiting integration audit;
  they are NOT 204 independently verified end-to-end capabilities. This checkpoint
  newly verifies three authentication/security requirements at API integration level.
- Setup/API details: docs/CONTROL_PLANE.md. No credentials invented, no real Groww
  validation, no PAPER performance or M1/M2 claim. User prev_chat.txt untouched.

### Previous next task (completed in part below)

Build the PAPER execution vertical slice using the existing PaperBrokerProvider
and database schemas: durable idempotent order intent, fresh deterministic
preflight/latch/config checks at dispatch, broker acknowledgement/unknown recovery,
fill/position synchronization, stops/targets/exits, journal/audit/P&L with explicit
cost provenance, then real scheduler and dashboard actions. Do not add a public
order bypass or treat advisory approval as dispatch permission. Verify negative
paths and restart recovery before declaring any execution requirement complete.
Keep RISK re-arm blocked in real deployments until trusted reconciliation exists.

## PAPER execution integration checkpoint (after local 6892570)

- Actual shared pipeline approval now feeds a durable CASH PAPER lifecycle:
  replayed approval/active config -> fresh broker-account/depth preflight ->
  persisted intent -> existing broker -> idempotent fills/position -> monitor
  stop/target -> signed exit -> gross P&L -> completed journal -> audit ->
  authenticated workspace API. React adds Journal and preflight decision views.
- Ten initial pages now read real persisted state. No public raw-order bypass,
  fake market stream, live credentials or remote push added.
- One account-wide lifecycle reservation is deliberately conservative. Unknown
  orders survive restart, block entries and are only looked up, not resubmitted.
  Partial fills, cancellation-before-exit, emergency exit, stale quotes, disabled
  strategy, altered approval, lost acknowledgement, broker failure, database failure,
  daily/drawdown latches and restart with a pending/part-filled position are tested.
- Reconciliation now supplies real PAPER quantity/order evidence, but full balance
  reconciliation/discrepancy resolution and multi-worker fencing remain partial.
  Do not equate this with all reconciliation requirements being satisfied.
- Integration found/fixed existing broker defects: lost fill history on restore,
  missing restored timestamps/config/PRNG state, zero-cash restoration inventing
  capital, reducing exits incorrectly charged new-entry margin, and premature
  average-price rounding. Broker-state revision CAS prevents stale snapshot writes.
- SQLite exposed an IST-vs-UTC persistence bug hiding account snapshots from the
  real API. OMS timestamps now persist UTC and response timestamps are explicit.
- All execution/portfolio/journal/frontend requirements touched here remain
  **partial**, not blanket completed. Costs and FIFO are incomplete; net P&L is
  unavailable. A stored stop is not labelled protected without a running worker.
  A critical alert and entry block report that limitation rather than fake safety.
- Details and exact operational boundaries: docs/PAPER_EXECUTION.md.
- Verification: **795 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **3 frontend passed, 0 failed**, production build/type generation pass. New
  execution/API/storage modules pass focused Ruff; whitespace checks pass.
  Ledger: **204 historical tested marks / 13 implemented-unverified / 64 partial /
  252 not started**. No additional whole requirement promoted to complete here.
  End-to-end autonomous PAPER, M1/M2, external notifications and Groww LIVE remain
  unverified. This is a verified integration checkpoint, not final completion.

### Previous next task (execution supervision now added below)

Connect a real single-worker intraday application service to the existing market
calendar, persisted/live/replay data adapters, quantitative analysis and a real
reference-strategy hypothesis. It must produce context/evidence for the shared
pipeline, call this executor, supervise protection ticks, run EOD/recovery, publish
worker health and expose authenticated stop/exit controls. No production synthetic
feed or client-provided portfolio. Keep the worker fail-closed when capital,
calendar, data, regime or contract-cost evidence is unavailable. Then integrate
configurable transaction costs/FIFO and durable fill/exit notifications, and run
the full twelve-scenario vertical-slice acceptance before claiming E2E completion.

## Opt-in execution supervisor checkpoint (2026-09-21)

- FastAPI lifespan now starts/stops an opt-in APScheduler PAPER supervisor with
  coalesced, non-overlapping cycles and host-local exclusive process ownership.
  It performs recovery, consumes actual persisted pipeline approvals, reads real
  recorded LIVE ticks, calls fresh execution preflight, monitors exits, applies
  session/no-entry/cutoff rules, and requests cancellation/MIS square-off.
- Actual database heartbeats expose disabled/unavailable/degraded/stale state to
  the dashboard. A worker error remains entry-blocking across process restart;
  it is not silently re-armed by a later successful cycle.
- Calendar incompleteness is now a hard entry blocker for this worker. Current
  bundled 2026 holiday data is incomplete. Missing ticks/depth remain unavailable,
  never replaced by generated prices/depth. PAPER_WORKER_ENABLED defaults false.
- Scope remains partial: this is execution supervision, not a complete upstream
  market ingestion -> analysis -> production reference-strategy cycle. Tests use
  isolated fixture proposals/data through the real application services. Protection
  certification, fees/FIFO, worker reset control, UI emergency actions and durable
  outcome notifications remain unfinished. No new entire requirement marked done.
- The prior docs' "no scheduler" statement is superseded by this supervisor, not
  by a claim that the user's full vertical slice now exists. See PAPER_WORKER.md.
- Final verification: **799 backend passed, 0 failed, 3 skipped**, two existing
  dependency warnings; **3 frontend passed, 0 failed**, production build passes.
  Worker tests include actual APScheduler callback execution. Focused Ruff and
  git whitespace checks pass. PostgreSQL integration skips still require a real
  configured test server; no external verification is inferred.
- Ledger: **204 historical tested marks / 13 implemented-unverified / 70 partial /
  246 not started**. Ten initial connected UI views; auth/risk/workspace APIs are
  real, but no authenticated order-action endpoint has been claimed implemented.
  PAPER stays default; Groww LIVE, full E2E, M1 and M2 remain unverified.

### Exact next task

Connect actual market ingestion and a production reference-strategy hypothesis to
analysis/context construction and the shared decision pipeline inside the worker.
Persist the evidence, use real point-in-time regime/data/cost availability, and
stand down on missing inputs. Then connect authenticated emergency/exit controls,
continuous-protection watchdog and audited worker-error recovery, configurable
Indian costs/FIFO and durable outcome notifications. Finish all twelve vertical-
slice acceptance scenarios, including UI/API state, before any E2E/M1/M2 claim.

## Windows startup repair (after 6aefb01)

- Reproduced the configuration defect: the distributed `STARTING_CAPITAL=` failed
  Decimal parsing before migrations or FastAPI could start. Empty/whitespace
  capital now normalizes to `None`; invalid, nonfinite, zero and negative values
  remain rejected. No capital is invented and risk readiness remains blocked.
- Verified the actual example dotenv and process-environment regression paths.
  Full backend suite: **808 passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  two existing dependency warnings. Frontend unchanged, not rerun for this repair.
- No requirement promoted. Ledger remains **204 historical tested marks / 13
  implemented-unverified / 70 partial / 246 not started**. User's pre-existing
  README edits and untracked `prev_chat.txt` are excluded from this checkpoint.
- Next: provider ingestion and deterministic reference strategy feeding the shared
  pipeline, then protection/costs/FIFO and the complete lifecycle acceptance tests.
  PAPER remains default; external Groww, full E2E and M1/M2 remain unverified.

## PAPER protection fault response (after a9fbf6d)

- Monitoring now independently compares open position protection against the
  audited/replayed approval and signed persisted fills. Missing or changed stops
  and targets generate an audit event, durably latch risk, reconcile, and request
  an emergency PAPER exit. Quantity/approval lineage discrepancies block instead
  of guessing an exit quantity. Stale data never fabricates an emergency fill.
- Restart tests remove/change protection and corrupt position quantity; the
  watchdog detects the former and reconciliation rejects the latter. The error
  latch survives restoration and requires explicit audited recovery.
- Full backend verification: **813 passed, 0 failed, 3 skipped** (two existing
  dependency warnings). Frontend unchanged. No requirement promoted: watchdog
  heartbeat certification, orphan-position recovery, comprehensive emergency UI
  and full E2E remain partial. Ledger counts remain 204 / 13 / 70 / 246.
- Next work underway: closed-candle provider ingestion and the production
  reference-strategy hypothesis. Full upstream decision-context construction,
  costs/FIFO and worker wiring are still needed; do not call this E2E complete.

## Provider-driven reference lifecycle checkpoint (after 438b355)

- Added `ReferenceIngestion` using the existing market-data provider interface,
  CandleStore, SMA20/ATR14 and hash-chained observation audits. Closed contiguous
  candles and fresh identity/provenance-checked depth are mandatory. Rejected
  input produces an audit record rather than silently substituted market data.
- Added one documented, fixed-parameter equity breakout hypothesis under the
  existing Strategy contract, registry and engine. No orders or quantities come
  from the strategy. Confidence is explicitly rule completeness, not probability.
- `ReferenceDecisionService` connects provider ingestion and that strategy to
  the existing shared proposal validation/sizing/risk pipeline. MARKET evidence
  references resolve verified, timestamped observation audits, not invented news
  or unrelated fundamentals. A complete session calendar is required.
- Real application integration tests now pass the same external provider fixture
  through ingestion/analysis/strategy/decision/preflight/PAPER fill/position/
  watchdog/target exit/gross P&L/journal/audit/authenticated workspace state. They
  also exercise disabled strategy and risk-latch rejection; invalid quote/candle
  inputs fail before storage. No internal pipeline or broker layer is mocked.
- **This is not the requested full vertical-slice milestone.** The test supplies
  trusted regime, blackout, account and cost context. Automatic runtime assembly
  and the scheduled worker connection are not implemented yet. Net P&L/costs,
  FIFO, full exit-policy supervision, notification delivery and UI lifecycle
  assertions remain pending. Existing dashboard polling remains unchanged.
- Full backend suite: **823 passed, 0 failed, 3 PostgreSQL-dependent skipped**,
  two dependency warnings. Frontend: **3 passed, 0 failed**, production build
  successful. Focused Ruff and whitespace checks pass.
- STRAT-005 moves from not started to **partial**, not complete. Ledger: **204
  historical tested marks / 13 implemented-unverified / 71 partial / 245 not
  started**. No new `[✓]` claim. PAPER default and Groww LIVE unverified remain.

### Exact continuation task

Wire `ReferenceDecisionService` into `PaperWorker` using a real server-owned
context builder: reconciled PAPER account, active risk limits, persisted regime
and event-calendar evidence, configured contract/cost evidence and fresh provider
observations. Do not reuse a stale DecisionContext or invent missing fee/regime
inputs. Add per-closed-bar durable deduplication/restart recovery and a worker-
driven integration test. Then implement Indian costs/FIFO, remaining protection
supervision and emergency UI. Full E2E/M1/M2 and external services remain unverified.

## Scheduled reference producer and owner controls (after 2fc353d)

- `ReferenceRuntime` now connects the real provider/ingestion/reference service
  to `PaperWorker`. Context is freshly assembled from the reconciled PAPER
  account, active risk configuration, persisted point-in-time regime/calendar
  and explicitly published contract-cost/restriction evidence. It never reuses
  a fixture portfolio or stale DecisionContext. Missing inputs stand down.
- With credentials, opt-in startup connects the existing live provider and uses
  it for both reference observations and execution quotes; without credentials,
  production reference data remains unavailable. No external verification claim.
- Durable per-instrument/version/origin/closed-bar claims prevent repeated
  decisions across polls and restart. Interrupted production blocks dispatch of
  a possibly approved proposal pending review rather than silently resuming.
- Tests drive the worker through provider input, reference approval, preflight,
  entry/fill, position, watchdog, target exit, gross P&L, journal and real workspace
  API. Restart deduplication, interrupted production and absent cost evidence are
  tested. A fractional-minute history-window bug was fixed by aligning requests
  to closed minute boundaries. Stopped-worker gates clear only on successful start.
- Existing Strategies UI now calls authenticated PAPER-only register, enable and
  sourced-input publication endpoints. Registry mutations and source publication
  are durably owner-attributed and audited. New registrations default disabled.
  OpenAPI/types regenerated with `backend/scripts/export_openapi.py` and npm types.
- Verification: **827 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **4 frontend passed, 0 failed**, build successful; focused Ruff passes. No
  requirement promoted to complete. Counts remain **204 historical tested marks /
  13 implemented-unverified / 71 partial / 245 not started**.
- Limitations: current-year calendar completeness, live source credentials and
  genuine regime/event/contract inputs remain required. Continuous source
  refresh, net fee/FIFO accounting, emergency actions, interrupted-cycle/worker
  reset and durable notifications remain unfinished. Full E2E/M1/M2 unverified.

### Next coherent unit

Integrate explicit versioned Indian transaction-cost estimates into actual PAPER
fills/account state/journal (unknown tariffs stay unavailable), then FIFO lot
accounting and authenticated emergency/recovery controls. Cost-model work has
started separately; it is not included in the runtime verification counts above.

## Audited PAPER fee estimates (after 67c706d)

- Explicit source/version/effective-date CASH/MIS fee schedules are published by
  the authenticated owner through the Strategies API/UI. No production rates
  are invented. Unsupported/missing tariffs remain unavailable.
- Cumulative per-order components prevent duplicate brokerage caps/minima across
  partial fills. Persisted PAPER broker snapshots retain costs through restart;
  OMS fills, positions, cash/equity, daily-loss input, journals and workspace API
  consume the actual charges. Gross and estimated net P&L remain distinct.
- Deterministic preflight includes configured entry/exit cost estimates and
  rejects an over-budget order before a broker call. Entry tariff is frozen with
  the intent. Tests caught an exchange-selection integration defect, now fixed.
- Verification: **838 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **4 frontend passed, 0 failed**, production build successful. Focused cost/risk
  integration tests rerun after refactoring passed. Two dependency deprecations
  remain. No Groww/external billing verification performed.
- PNL-003 moves from not started to partial, not complete: CASH/MIS estimates
  work, but other products/segments and contract-note rounding remain pending.
  Counts: **204 historical tested / 13 implemented-unverified / 72 partial /
  244 not started**. See `docs/PAPER_COSTS.md` for scope and primary references.
- Next coherent unit: integrated FIFO lots in broker and OMS, exact partial-exit
  arithmetic, persisted matching/restart verification; then emergency/recovery
  controls and full worker lifecycle coverage. FIFO work is in progress separately
  and is not included in this checkpoint's verification or completion claims.

## Integrated PAPER FIFO accounting (after 1aaeadb)

- Broker and OMS use one exact signed-unit FIFO matcher. Persisted remaining
  lots, matching IDs and sequence numbers preserve partial-exit accounting over
  restart. Cumulative rounding avoids per-fill penny drift; partial realized
  profit now reaches daily-loss input before a position becomes flat.
- Reconciliation compares cost basis, gross realized profit, charges and cash,
  not just quantity. Corrupt/legacy open snapshots without FIFO evidence fail
  closed pending reviewed reconstruction. No invented opening lots.
- The real scheduled provider-to-reference-to-risk-to-PAPER-to-exit test now also
  runs with an explicit synthetic tariff, net journal P&L and restart deduplication.
  Actual partial-exit recovery, journal/API and fill-level matching are verified.
  Existing Orders UI exposes persisted fills, costs and FIFO attribution.
- Verification: full suite **853 passed, 0 failed, 3 skipped**; subsequently
  added cost-basis discrepancy regression and FIFO tests **15 passed**, bringing
  verified backend coverage to **854 tests**. Execution/cost regression batch
  **18 passed** after reconciliation hardening. Frontend **4 passed**, build
  successful. PostgreSQL-specific tests remain skipped; no external verification.
- PORT-002 moves to partial; PNL-001 remains partial (generalized multi-position
  execution and derivative-specific acceptance are not complete). Counts:
  **204 historical tested / 13 implemented-unverified / 73 partial / 243 not
  started**. See `docs/PAPER_FIFO.md` for legacy recovery limitations.
- Exact next unit: authenticated emergency controls with durable entry inhibition,
  safe flatten and real UI outcomes, followed by explicit health-gated recovery.
  This next unit is being developed separately and is not included in the FIFO
  checkpoint's requirement or verification claims. Full E2E/M1/M2 remain pending.

## Authenticated PAPER emergency and reviewed recovery (after c417f85)

- Existing Risk UI now calls typed, authenticated emergency API actions: kill,
  inhibit entries, flatten, health-gated clear and explicit worker review. State
  mutations/flatten outcomes are durably attributed to the owner. Entry inhibition
  survives restart and is checked during shared risk authorization and dispatch.
- The full suite caught a genuine authority-boundary defect: importing mutation
  controls from risk made them reachable from advisory code. Read-only emergency
  state checking now lives in `risk/emergency_state.py`; the existing transitive
  no-execution-authority test passes without weakening its assertion.
- Flatten uses actual OMS cancellation/exits, reports per-item persisted status
  and remaining quantity, and never claims unavailable/stale execution succeeded.
  Explicit owner flatten can replace a known terminal cancelled/rejected exit
  after reconciliation, only for remaining quantity. Pending/unknown exits are
  never blindly replaced. Journals retain all exit attempts and weighted fills.
- Worker review requires fresh passing critical checks and a flat reconciled
  account. Unexecuted approvals are marked blocked, interrupted claims receive
  audited stand-downs, and the worker-error heartbeat is cleared transactionally.
  No interrupted intent is replayed; other risk/emergency blockers remain active.
- Verification: **863 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **5 frontend passed, 0 failed**, production build successful; focused Ruff and
  diff checks pass. OpenAPI/types synchronized. Two dependency deprecations remain.
  No credentials, real notification delivery or Groww LIVE verification claimed.
- FE-016 and EMG-001/002/003/006/007 move to partial, not complete. Counts:
  **204 historical tested / 13 implemented-unverified / 79 partial / 237 not
  started**. No new complete marks; PAPER remains default. Documentation updated
  to remove obsolete claims that no strategy, dashboard, fees or FIFO exist.
- Remaining limits: no emergency CLI, pending exits retained rather than full
  cancel/replace, no automatic execution of an unavailable-worker flatten, refusal
  audit parity incomplete, and continuous protection/source refresh unverified.
  M1/M2 and the complete notification-inclusive vertical slice remain incomplete.

### Exact next high-value unit

Persist PAPER lifecycle notifications with transactional audit linkage and bounded,
restart-aware background delivery using the existing notification abstraction.
Expose pending/failed/acknowledged (not externally verified) state in the existing
Monitoring view, and extend the real worker lifecycle test through notification
failure/recovery. Then close the continuous protection/source-refresh gaps and
remaining PAPER acceptance paths before starting the backtesting layer.

## Durable PAPER lifecycle notifications (after 33e92dd)

- PAPER order-state/rejection/stop-fill and emergency activation requests are
  persisted transactionally in the existing hash-chained audit ledger. Stable
  keys prevent repeated synchronization from duplicating logical requests;
  source audit/order/proposal/position IDs retain reconstruction links.
- A separate background dispatcher uses existing configured channel interfaces,
  routing, quiet hours and admission limits. Claimed attempts/receipts survive
  restart, acknowledged channels are not resent, and attempts are bounded.
  Interrupted sends wait for claim expiry; ambiguous transport outcomes can still
  duplicate messages and are not represented as exactly-once or verified delivery.
- The provider-driven reference/fee/FIFO PAPER trade test now reaches persisted
  notification requests, isolated transport acknowledgements and workspace state.
  Failure/recovery, exhaustion, interrupted-send restart and altered-audit refusal
  are covered. Delivery failures do not change positions or stop the PAPER worker.
- Existing Monitoring view displays actual pending and per-channel outcomes,
  attempt counts and audit linkage. Missing channels are degraded/disabled, not
  connected. No new static pages or fake delivery successes were introduced.
- A genuine teardown defect (cancelling SQLite connection startup) was found and
  fixed using bounded graceful outbox shutdown. Full tests now also treat
  unhandled thread warnings as errors.
- Verification: **868 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **5 frontend passed, 0 failed**, production build successful; focused Ruff and
  diff checks pass. Only the two existing dependency deprecations remain.
- No requirement is promoted to complete. NOTIF-003 stays partial: other mandatory
  producers and externally verified delivery remain outstanding. Counts unchanged:
  **204 historical tested / 13 implemented-unverified / 79 partial / 237 not
  started**. PAPER default; Groww LIVE and M1/M2 remain unverified/incomplete.

### Exact next high-value unit

Persist independently verified watchdog observations and expose their freshness
in the existing Positions/Monitoring API/UI, including startup/restart and stale
monitor negative paths. Check PAPER exchange identity while tightening recovery:
the older broker-position projection hardcodes NSE and must not mislabel BSE.
Then connect continuous point-in-time regime/source refresh and finish remaining
PAPER acceptance paths before beginning backtesting. Do not call the complete
vertical slice certified merely because the positive fixture now reaches notices.

## PAPER watchdog recovery and exchange identity checkpoint

- Startup now independently validates open-position stop/target lineage. A missing
  stop latches risk, attempts a valid-quote emergency exit and refuses normal
  worker startup. Pending-fill processing is followed by reconciliation and a
  second protection check, rather than relying on the pre-fill quantity.
- Successful checks persist quantity, stop/target, quote provenance and bounded
  expiry in the position audit chain. Positions API/UI reports recent/stale,
  changed, failed, unavailable or integrity-failed evidence. The scheduler's
  separate Monitoring state remains authoritative for worker availability;
  `is_protected=False` is retained, not converted into a broker-stop guarantee.
- Monitor risk observations now carry the actual current quote timestamps/depth,
  rather than just advancing the original proposal's `as_of` timestamp.
- PAPER position exchange projection derives exchange from actual filled-order
  lineage and refuses absent/ambiguous lineage. Reconciliation includes exchange
  identity. BSE new orders remain explicitly unsupported, not newly enabled.
- Verification: **873 backend passed, 0 failed, 3 PostgreSQL-dependent skipped**;
  **5 frontend passed, 0 failed**, production build successful. Focused lint and
  diff checks pass. Tests cover expiring evidence, stale monitoring, restored
  state, startup missing protection, altered audit evidence and exchange identity.
- REC-006 moves from not started to partial; no requirement is newly complete.
  Counts: **204 historical tested / 13 implemented-unverified / 80 partial /
  236 not started**. PAPER remains default. Groww LIVE is unverified. Full PAPER
  acceptance and M1/M2 remain incomplete; software monitoring cannot guarantee
  stop execution during process/network outage or absent valid prices.

### Exact next high-value unit

Connect configurable, sourced regime production to provider-fed reference cycles:
closed index candles through existing technical analysis and persisted hysteresis,
with explicit timestamped IV/breadth/event evidence. Missing/stale external inputs
must yield UNKNOWN/stand-down, not fabricated normal conditions. Preserve lineage
into decisions/journals and expose the actual refreshed regime. Then complete the
reference exit-policy and remaining PAPER lifecycle acceptance before backtesting.

## Provider-fed regime integration checkpoint

- Optional owner-audited `regime_source` now selects a real stored INDEX and
  explicit indicator/classifier policy, IV, breadth and calendar evidence.
  Reference cycles ingest closed index candles through the existing provider,
  persist source snapshots/candles and use existing technical functions and
  durable regime hysteresis. No fixture is installed as a production provider.
- The resulting regime ID flows into the decision and frozen journal context;
  journal `regime_at_entry` is populated. Dashboard API/UI includes actual label,
  candidate, underlying, origin and timestamp, with stale/incomplete states.
- Integration found a genuine stale-evidence gap: a recent regime/decision could
  outlive one of its sources. Strategy and shared-pipeline gates now validate
  underlying feature timestamps. Entry preflight and final dispatch recheck
  regime/contract evidence and event coverage; no timestamp is renewed to make
  expired input appear current. Expiry at dispatch produces no broker call.
- Tests use actual index ingestion, worker, shared pipeline, costed PAPER fills,
  journal and API; missing/stale breadth, future index bars, future publication
  and source expiry before/after risk approval are exercised. Frontend tests
  assert historical TRENDING_UP is visibly stale when its evidence has expired.
- Verification: final full backend run **883 passed, 0 failed, 3 PostgreSQL-only
  skipped**; frontend **5 passed, 0 failed**, build successful. Focused Ruff passes.
  One older lifecycle fixture was corrected to supply actual calendar evidence
  instead of merely claiming CLEAR; missing-calendar execution is explicitly
  tested as a refusal, not allowed through a weaker preflight.
- Follow-on Windows startup repair: Alembic now escapes literal percent signs
  for ConfigParser, preserving encoded database URLs. The defect was reproduced;
  real migration tests now include a percent-containing database path. After this
  one-line change, **10 startup/migration and 38 configuration tests passed**.
  This adds one test beyond the full-run count. `docs/WINDOWS.md` gives exact
  non-Docker commands, private configuration and truthful service prerequisites.
- Counts unchanged: **204 historical tested / 13 implemented-unverified /
  80 partial / 236 not started**. No new complete mark. External IV/breadth/event
  publication remains manual, and index history/live connectivity remain
  externally unverified. This is entry-candidate refresh, not continuous
  whole-market analysis while a position is open. PAPER remains default.

### Exact next high-value unit

Integrate the reference strategy's existing deterministic trailing/time/
invalidation exit policy into actual open-position monitoring, with persisted
favourable-price state, restart continuity, source-grounded invalidation and the
same PAPER exit/fees/FIFO/journal/audit path. Preserve immediate stop/target
handling even when analytical input fails. Then wire periodic critical health
checks and finish remaining PAPER recovery acceptance before backtesting.

## Reference exit-policy integration checkpoint

- Real open reference positions now use the existing deterministic exit evaluator
  for trailing, holding time and closed-candle/SMA invalidation. All exit reasons
  converge on the existing idempotent PAPER OMS, fees/FIFO, journal and notices.
- Favourable-price state and trailing thresholds persist in the position audit
  chain and survive restart. Changed/missing state is rejected. Analytical
  outages never invent a false invalidation: evidence completeness is explicit,
  while valid quotes still permit independently justified trailing/time exits.
  Fixed stop/target handling runs first and remains available during degradation.
- Failure audit, durable notification request and failed worker heartbeat commit
  together, closing the crash-before-normal-heartbeat recovery gap. Owner review
  remains required even if a valid risk-reducing exit subsequently closes the trade.
- Actual accounting and risk observations refresh after analytical exits; the
  dashboard no longer retains pre-exit exposure. Positions show the persisted
  trailing threshold and journal API exposes frozen entry regime ID/label.
- Focused lifecycle checks: 8 reference-exit scenarios pass, plus shared exit,
  execution, regime, worker and outbox coverage. Frontend: 5 passed and production
  build successful. Final full backend verification: **893 passed, 0 failed,
  3 PostgreSQL-dependent skipped**. Focused Ruff and diff checks pass; only the
  two existing dependency deprecations remain.
- REG-005 is now integrated/tested through provider-fed decisions, actual journal
  closure and the typed API. Conversely HD-008 was downgraded on inspection:
  replay exposes an entire OHLC bar at its opening timestamp and re-dates old
  prices to the current cursor. Existing tests check cursor bounds, not bar
  closure/freshness. The production reference ingestor rejects open candles, but
  replay itself must be corrected before replay/backtesting is trusted.
- Counts remain **204 tested / 13 implemented-unverified / 80 partial / 236 not
  started** (one justified promotion, one honest downgrade). EXEC-009 remains
  partial outside the scoped reference/PAPER path. No full vertical-slice, M1/M2,
  unattended protection, external notification or Groww LIVE certification.

### Exact next high-value unit

Correct replay bar-close availability and original quote timestamps with adversarial
multi-instrument/interval tests; then wire periodic critical health checks into
the running application, including non-critical news degradation and durable
failure reporting. Complete the remaining PAPER acceptance matrix before backtests.

## Replay availability and runtime health checkpoint

- Corrected replay to publish nominally closed bars, preserve original quote
  timestamps across instruments and publish simultaneous observations before
  callbacks. Invalid/overlapping candles and conflicting interval closes fail
  before publication. Callback failures prevent silent continuation.
- FastAPI now starts a periodic health watchdog before the PAPER worker and
  stops it before database teardown. Critical unavailable/skipped/degraded
  checks and missing required checks block entries. Recovery cannot clear risk,
  emergency or worker-review latches. Healthy-state audit failure also blocks.
- Health transitions and durable notification requests commit atomically.
  Non-critical unavailable news is explicitly degraded, without globally
  disabling non-news strategies. External delivery is not verified.
- The first full run exposed an obsolete boot test expecting healthy trading
  without market data or an external clock. Updated the expectation to 503 and
  asserted the named blockers; no safety gate was weakened. Focused boot and
  watchdog regression: 15 passed. Replay/health focused batch: 66 passed.
  Frontend: 5 passed; production build successful. Full backend rerun:
  **913 passed, 0 failed, 3 PostgreSQL-dependent skipped**. Only the existing
  two dependency deprecations remain; focused Ruff and diff checks pass.
- HD-008 restored to tested after adversarial provider-interface checks.
  MON-005 and MON-009 are partial: periodic enforcement and transition history
  exist, but complete runtime-provider check coverage, historical range API and
  independent process monitoring remain outstanding. Counts: **205 tested /
  13 implemented-unverified / 81 partial / 234 not started**.
- See `docs/HEALTH_WATCHDOG.md` for timing, SQLite, audit-outage and replay-model
  limitations. No full-day replay, complete browser E2E, M1/M2, live connectivity
  or Groww LIVE verification claim.

### Exact next high-value unit

Audit the health registry's broker identity: a PAPER adapter ping must never be
reported as verified Groww credentials. Then complete real-browser verification
against the actual PAPER API lifecycle and finish remaining runtime-check/recovery
acceptance before backtesting. Do not replace unavailable dependencies with data.

## Broker health identity correction

- Inspection confirmed a genuine misreporting defect: with configured credential
  fields and the PAPER adapter selected, its simulated `ping()` could produce
  "credentials verified" in the Groww health check. Wrong/simulated adapters now
  cannot reach that probe or claim verification. Real-broker modes fail closed;
  PAPER reports authentication UNVERIFIED without requiring live execution.
- A successful real-adapter read-only probe explicitly leaves live execution
  UNVERIFIED. No credentials or external calls were used for this verification.
- Focused identity, health and application-boot tests: **38 passed, 0 failed,
  0 skipped**. Previous full suite: 913 passed / 0 failed / 3 skipped; frontend
  5 passed and build successful. Eight new isolated identity cases are included
  in the focused count, not retroactively claimed as a full-suite run.
- AUTH-004 remains implemented/unverified; counts unchanged: **205 tested /
  13 implemented-unverified / 81 partial / 234 not started**.
- User-owned README, migration edit and `prev_chat.txt` remain outside commits.

### Exact next high-value unit

Add actual browser-to-backend PAPER lifecycle verification using the existing
reference-worker fixture and production React bundle/API, with simulated external
market input confined to tests. Verify login, position/protection, exit, net
journal and audit state without adding another dashboard or toy pipeline.

## Actual browser-to-PAPER lifecycle checkpoint

- Added an opt-in headless Edge test using the production React build, real HTTP
  API, real owner login and existing reference-worker fixture. No browser API
  responses are mocked and no fixture endpoints are added to production code.
- Browser observes the actual 111-unit entry, independently recorded software
  protection check, executed order/fill, then the target exit, zero exposure,
  journal gross/charges/net, audit rows and durable notification requests.
  Fixture values are mathematical test expectations, not market-performance evidence.
- Test setup permits only its exact loopback origin; authentication and CSRF
  enforcement remain active. The deployed startup/health lifecycle is
  tested separately, not bypassed and then claimed externally verified.
- Real browser scenario: **1 passed**. Frontend component suite: **5 passed**;
  production build successful. Full backend suite with browser opt-in enabled:
  **922 passed, 0 failed, 3 PostgreSQL-dependent skipped**. Existing two dependency
  deprecations remain; no test-thread errors. Browser artifacts remain test-only.
- TEST-017 is now genuinely implemented/tested/integrated against its scoped
  chain acceptance criterion. Counts: **206 tested / 13 implemented-unverified /
  81 partial / 233 not started**. This is not a claim that all PAPER acceptance,
  all browser negative paths, full-day replay, M1 or M2 are complete.
- See `docs/PAPER_BROWSER_VERIFICATION.md` for repeatable commands and limitations.
  No new pages were added; the existing 10 views consume the same actual APIs.

### Exact next high-value unit

Exercise authenticated emergency controls and blocked/stale states through the
actual browser/API, then connect runtime PAPER account/order/protection health
to the active recovered worker rather than detached startup broker checks.
Finish this remaining PAPER acceptance before starting the backtest engine.

## Browser emergency acceptance checkpoint

- The actual browser now drives authenticated typed-confirmation FLATTEN through
  the real owner API and registered test worker. Incorrect confirmation disables
  the action; valid confirmation closes through existing OMS/FIFO/cost/journal
  code. Repeating FLATTEN creates no duplicate order.
- A deliberately stale external quote leaves the exit OPEN and the actual
  position unchanged. The UI reports blocked entries and the pending outcome;
  no simulated fill, flat position or journal is invented. Existing implementation
  already handled this correctly; tests now verify it through the browser.
- Browser scenarios: **3 passed, 0 failed, 0 skipped**. Emergency service/API
  regressions: **5 passed, 0 failed, 0 skipped**. Latest full checkpoint remains
  922 passed / 0 failed / 3 PostgreSQL-dependent skipped, before the two new
  browser cases. Frontend component tests/build unchanged and previously verified.
- Requirements stay scoped/partial rather than claiming every emergency or
  dashboard requirement complete. Counts unchanged: **206 tested / 13
  implemented-unverified / 81 partial / 233 not started**.

### Exact next high-value unit

Connect runtime PAPER account/order/protection health to the active recovered
worker rather than detached startup broker checks. Persist meaningful heartbeat
and missing/stale worker states, preserve risk-reducing exits, and verify entry
enforcement without treating missing external data as healthy. Then finish the
remaining PAPER acceptance matrix before backtesting.

## Active PAPER runtime health checkpoint

- Added a read-only order-service health check tied to the actual recovered
  worker, durable cycle heartbeat, unknown order state and independently audited
  protection evidence. It reports actual simulated cash/margin, not a detached
  startup account. Enabled-but-absent/stale/unrecovered execution fails closed;
  explicitly disabled PAPER execution is honestly SKIPPED.
- The check never places orders or acquires the cycle lock. This matters because
  authenticated emergency clear/worker review already hold that lock when they
  ask for health checks. Tests exercise the check while that lock is held.
- Availability does not override risk/owner-review latches or certify fresh
  independent reconciliation. All check names now appear at application boot;
  unavailable news and unsupported real-broker order health remain explicit.
- Health exceptions now expose their class, not uncontrolled exception text.
  Focused runtime/watchdog/boot/health suite: **39 passed, 0 failed, 0 skipped**.
  Full backend/browser rerun: **928 passed, 0 failed, 3 PostgreSQL-dependent
  skipped**. All three actual browser scenarios pass. No frontend source changed; existing views
  display the resulting real health rows through their current API.
- MON-007 remains partial: PAPER heartbeats and stale detection are integrated,
  not an independent external process watcher. Counts unchanged: **206 tested /
  13 implemented-unverified / 81 partial / 233 not started**.

### Exact next high-value unit

Complete sustained-session evidence production before the historical runner.
Inspection found that `ReferenceInputs.costs` also carries margin/notional and
currently expires with the market-data TTL, while regime IV/breadth are explicit
one-time observations. Their rejection on expiry is correct, but without fresh
producers a configured worker cannot keep generating candidates through a session.
Derive new PAPER contract observations from actual fresh quotes and the explicit
simulated margin/effective fee models; retain original source/validity dates for
external restrictions and regime observations. Add provider-backed IV/breadth
refresh only when real inputs exist; missing inputs must still stand down. Then
verify multi-cycle/full-session and restart acceptance. Never simply re-date old
evidence or relax freshness limits to keep the loop trading.

## Fresh PAPER contract production checkpoint

- The actual reference worker now derives CASH/EQUITY margin/notional evidence
  from fresh ingested quotes and the recovered simulated account, using an
  explicit effective tariff and bounded owner policy/restriction attestation.
  Original quote, tariff and policy dates are preserved and auditable. Missing
  tariffs or expired attestations stand down without an order.
- Shared deterministic sizing incorporates quantity-dependent round-trip fees.
  Testing identified that one-lot brokerage minima incorrectly overestimated
  per-unit reserves for larger positions; the shared sizer now performs bounded,
  conservative re-sizing using the actual candidate quantity. Final execution
  preflight remains independent and authoritative. No fee defaults are invented.
- Policy/tariff expiry is enforced in the shared pipeline, submission and final
  entry dispatch. Existing legacy cost-input freshness behavior is unchanged.
  See `docs/PAPER_CONTRACT_INPUTS.md` for publication and scope.
- Integration tests prove two worker trades beyond the legacy evidence TTL,
  different fresh margin/notional observations, unchanged policy lineage,
  hand-computed quantities/charges, missing-input stand-down, expiry boundaries
  and authenticated publication. Separate fresh regime fixtures remain explicit;
  this does not yet prove automatically refreshed full-session regime inputs.
- Verification: **936 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including
  all three real browser scenarios. Frontend: **5 passed**, production build and
  generated API types successful. Changed Python files pass Ruff. Two existing
  dependency deprecation warnings remain. PostgreSQL migrations/live Groww and
  real notification delivery are not externally verified.
- Requirement status counts remain **206 tested / 13 implemented-unverified /
  81 partial / 233 not started**. SIZE-008 traceability is expanded; no broad
  PAPER, F&O billing or full-session requirement is prematurely promoted.

### Exact next high-value unit

Connect optional provider-backed regime breadth and option-chain IV producers
to the existing regime refresh path. Require explicit point-in-time constituent
and expiry configuration, preserve original observations and persist evidence.
Missing/stale/inconsistent provider inputs must still block entry, not renew old
manual observations. Then verify sustained worker cycles and restart with those
producers before moving to the historical runner.

## Provider-backed regime integration checkpoint

- Optional declared-constituent OHLC breadth and declared-expiry option-chain IV
  now enter the existing reference regime service through MarketDataProvider.
  Explicit source-policy knowledge/validity, identities, provenance, complete
  constituent coverage and source freshness are required. Manual observations
  cannot be mixed with the producer or used as a hidden fallback.
- Breadth includes unchanged constituents in its denominator. IV is the mean of
  both ATM legs in percentage units, lower strike on ties, not India VIX. Missing
  IV is unavailable/UNKNOWN; stale, future or inconsistent sources fail closed.
  Original Greek/quote timestamps are retained. Regime policy expiry propagates
  into persisted history and the shared consumption freshness gate.
- The existing audit chain retains sanitized raw observations, declaration and
  method names. Existing workspace APIs expose the resulting actual regime;
  no new static frontend page was added. See `docs/PAPER_REGIME_SOURCES.md`.
- Integrated fixture verification covers entry, costed exit and a second entry
  beyond the prior market-evidence TTL, with newly supplied closed equity/index
  bars and fresh provider observations but unchanged owner declarations. It also
  covers missing constituents, stale/future breadth, wrong origin, stale Greeks
  and missing IV. Focused regime suite: **27 passed, 0 failed, 0 skipped**.
- Full backend/browser regression: **943 passed, 0 failed, 3 PostgreSQL-dependent
  skipped**, including all three actual browser scenarios. Two existing dependency
  deprecation warnings remain. Generated API types and frontend production build
  pass. No frontend component source changed since its five passing tests.
- Counts remain **206 tested / 13 implemented-unverified / 81 partial / 233 not
  started**. REG-002 gains integration traceability, not a new blanket promotion.

### Exact next high-value unit

Verify recovery during an open provider-driven PAPER position and sustained
session transitions using these producers. Preserve persisted declaration/source
lineage, independent protection, no duplicate entries and final close/journal/API
state. Expand session acceptance to EOD and provider outage/recovery before
starting the historical execution runner. Full-session certification, PostgreSQL
recovery, real external feeds/delivery and Groww LIVE remain unverified.

## Provider-driven open-position restart verification

- Extended the provider-to-worker acceptance scenario to stop execution with an
  open position, instantiate a fresh executor/worker and restore persisted state.
  With a fresh post-entry provider quote, monitoring resumes without duplicating
  entry; the restored position exits and a later independently derived candidate
  executes using the same persisted source declarations.
- The first fixture attempt reused a quote observed before the entry fill. The
  exit-state chronology gate correctly refused it. The fixture now supplies a
  new deterministic provider observation; no production gate was weakened and
  no production code change was needed for this restart scenario.
- Focused provider/worker/reference-exit regression: **20 passed, 0 failed,
  0 skipped**; Ruff passes. Latest full backend/browser checkpoint is **943
  passed / 0 failed / 3 PostgreSQL-dependent skipped**, before this one additional
  restart parameter case. Frontend remains five passing tests and a passing
  production build. No external dependency was newly verified.
- REG-005 integration traceability expanded. Counts unchanged: **206 tested /
  13 implemented-unverified / 81 partial / 233 not started**.

### Exact next high-value unit

Extend provider-driven session acceptance through EXIT_WINDOW, EOD reconciliation
and provider outage/recovery, including truthful unresolved-position state when
fresh exit quotes are absent. Verify that no post-cutoff entry is created and
the next session cannot silently clear review/risk latches. Then progress to
the historical execution runner using the same sizing/risk/cost/accounting path.

## Provider-driven cutoff and EOD recovery

- The actual provider/reference strategy now has integration acceptance through
  cutoff, forced MIS exit, EOD reconciliation, restart and the next trading day.
  No strategy/chain refresh or new entry is performed at cutoff. Fresh quotes
  produce the costed exit/journal; stale quotes leave the existing position and
  pending exit intact. Restoring with a fresh quote closes that same exit without
  duplicating orders. The worker-review latch survives recovery and the next day.
- Fixed misleading worker detail: outside entry hours it now reports the actual
  session phase, open MIS position count and unresolved order count, rather than
  retaining an earlier strategy approval message. A latched worker continues to
  report review required after successful risk-reducing recovery.
- Session phase/failure transitions are audited in the heartbeat transaction.
  Repeated unchanged cycles do not append duplicate transition events. Existing
  authenticated workspace/monitoring views consume the truthful worker detail;
  integration tests verify scheduler status and resulting journal through the API.
- Focused cutoff/worker suite: **6 passed, 0 failed, 0 skipped**. Final API-state
  refinements: **2 passed**. Changed files pass Ruff. Full backend/browser suite:
  **946 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including all three
  actual browser scenarios; two existing dependency deprecation warnings remain.
  Frontend source/schema unchanged, with latest five tests and production build
  passing. No real feed, notification delivery or Groww LIVE validation occurred.
  This is session-boundary/recovery evidence, not continuous real-market paper
  validation. INTRA-006 stays partial for broader execution scope. Counts remain
  **206 tested / 13 implemented-unverified / 81 partial / 233 not started**.

### Exact next high-value unit

Begin the historical runner through the same production worker/pipeline. First
extend recorded-data replay to include point-in-time quote/depth, OHLC previous
close and option-chain snapshots: the current candle-only provider intentionally
cannot supply those observations and must not fabricate them. Preserve original
availability and source times, deny future reads and persist run provenance.
Historical execution must use isolated run/account state, never rewind a running
PAPER account/database or share process-global gates/clock with the live worker.
Then drive complete recorded sessions, costs/FIFO and rejection reporting before
adding walk-forward/OOS. Missing historical coverage must be explicit.

## Recorded snapshot replay integration

- Extended the existing replay provider with recorded quote/depth, session OHLC
  and explicit-expiry option-chain publications. Their original observation and
  availability times are distinct; same-time events publish before callbacks.
  Declared recorded streams never silently fall back to newer candle prices.
  Missing values stay unavailable. Returned data is REPLAY, not LIVE.
- Loading is atomic, rejects duplicate/regressing/malformed records and freezes
  after stepping starts. Input/returned-value mutation cannot alter the tape.
  Source identity, original origin and timestamps are persisted with consumed
  market/regime observations in the existing audit path.
- A real replay provider now drives the existing production reference worker,
  analysis/regime/strategy, shared decision/sizing/risk, PAPER OMS, entry/exit,
  fee/FIFO journal and API state. Only the input recordings are deterministic
  isolated fixtures; the trading services are not mocked. Focused verification:
  **23 passed, 0 failed, 0 skipped**. Changed Python files pass Ruff. Full backend
  and browser regression: **954 passed, 0 failed, 3 PostgreSQL-dependent skipped**,
  including all three actual browser scenarios. Two existing dependency warnings
  remain. Frontend source/schema is unchanged; latest five component tests and
  production build pass. The full run took 12 minutes; it was allowed to finish
  rather than restarted when observation waits elapsed.
- PAPER-008 is now partial, not complete: one recorded trade is integrated but
  there is no durable full-day runner yet. Counts: **206 tested / 13 implemented-
  unverified / 82 partial / 232 not started**. See `docs/RECORDED_REPLAY.md`.
  No profitability, external feed/delivery, Groww LIVE or M1/M2 claim is made.

### Exact next high-value unit

Add durable recording bundles with explicit availability/source times and content
hashes, coverage validation and loading into this provider. Then implement the
isolated historical run process/account and persisted run results through the
same worker. Do not reuse or rewind active PAPER state. Finish full-session replay
and no-lookahead/fill-convention acceptance before metrics and walk-forward.

## Durable recording bundle checkpoint

- Added versioned typed JSON recording files with explicit source/origin and
  availability semantics, canonical-content SHA-256, exclusive non-overwriting
  writes with fsync, and bounded 64 MiB reads/writes. Unsupported versions,
  duplicate JSON keys/series/publications, corrupt/truncated or oversized content
  fail closed. Hashes identify content, not external authenticity.
- The read-only `python -m app.marketdata.recordings <file>` command validates
  a file and reports actual coverage metadata. It does not execute trades or
  invent continuous coverage; gaps/session/universe completeness remain unchecked.
  Invalid inputs return code 2 without echoing uncontrolled file contents.
- Updated the real worker integration test to write, reload and execute from a
  durable bundle. Its digest is persisted with original recording provenance in
  the actual market/regime audit trail. Net journal and API state still derive
  from the existing strategy/decision/risk/OMS/accounting services.
- Focused recording/replay/ingestion/worker suite: **38 passed, 0 failed,
  0 skipped**. Changed Python files pass Ruff. Latest full backend/browser run
  remains **954 passed / 0 failed / 3 PostgreSQL-dependent skipped**, before this
  new bundle unit; broader rerun is due with the historical runner checkpoint.
  Frontend source/schema unchanged; latest five component tests/build pass.
- PAPER-008 stays partial. Counts unchanged: **206 tested / 13 implemented-
  unverified / 82 partial / 232 not started**. No external feed, Groww LIVE,
  notification delivery, full-session validation or M1/M2 verification is claimed.

### Exact next high-value unit

Implement isolated historical run/account bootstrap and a run manifest around
the durable recording provider and existing PAPER worker. Persist configuration,
recording hashes, progress and honest completed/blocked outcomes; never reuse an
active PAPER database or process-global clock/gates. Require explicit capital,
risk, fee, instrument, calendar and strategy/source policies. Verify a whole
recorded session and missing-data/no-lookahead rejection before exposing API/UI
run controls and adding metrics/OOS/walk-forward.

## Historical manifest and isolated account bootstrap

- Added validated historical manifests for recording hashes, explicit capital/
  risk/fees, point-in-time instrument dependencies, reference/regime inputs,
  calendar coverage and fill configuration. Future knowledge and missing required
  inputs fail closed; dates are mapped to Indian-market calendar dates.
- Preparation requires an empty, explicitly named historical database, no active
  worker, PAPER mode/broker, matching capital/hash and the installed historical
  clock. It never deletes or reuses existing account state. A transactional run
  reservation/audit chain prevents silently replacing another bootstrap.
- Existing cost/input publication, strategy registration and PAPER account
  recovery now prepare the actual shared worker/account. Status is PREPARED,
  never falsely completed. Post-reservation errors persist FAILED plus a sanitized
  failure audit. No entry gate is cleared and bootstrap creates no trade.
- Focused bootstrap/recorded-worker/bundle verification: **16 passed, 0 failed,
  0 skipped**. Ruff passes. Full backend/browser regression: **969 passed,
  0 failed, 3 PostgreSQL-dependent skipped**, including all three real browser
  scenarios and the earlier durable-bundle unit. Two existing dependency warnings
  remain. Frontend source/schema unchanged; latest five component tests/build pass.
- BT-013 is partial. Counts: **206 tested / 13 implemented-unverified / 83
  partial / 231 not started**. See `docs/HISTORICAL_RUNS.md`. Database naming and
  emptiness guards are not a child-process boundary: the launcher remains pending.
  No PostgreSQL, external feed, Groww LIVE or full backtest validation is claimed.

### Exact next high-value unit

Connect the prepared worker to the recording event timeline and persist progress,
actual journal-derived results and truthful incomplete/failed outcomes. Add the
dedicated child-process launcher before exposing historical execution to API/UI.
Pin execution/session settings, prevent post-window publication and complete
full-session/no-lookahead/fill-convention tests. Do not install a historical clock
or database into the serving application process. Metrics/OOS/walk-forward follow.

## Bounded historical worker execution

- Prepared runs now execute the actual PAPER worker against the recording event
  timeline, with cadence cycles between publications and a hard end timestamp.
  Warm-up cannot rewind the application clock, expose post-start publications,
  notify subscribers or continue after failed warm-up.
- The runner checks the dedicated database, original manifest/hash/capital,
  PAPER provider and pinned execution/session settings. Only the startup blocker
  is cleared after recovery; independent safety gates remain enforced.
- Progress and real journal-derived gross/charges/net results are persisted.
  Open positions/pending orders, source stand-downs and missing costs remain
  INCOMPLETE; worker failures remain FAILED. No forced end-window fill or
  fabricated metric is introduced. Exceptions stop the worker and record the
  error class when persistence is available.
- Focused historical/bootstrap/replay/recorded-worker regression: **30 passed,
  0 failed, 0 skipped**. Ruff passes. Last full backend/browser checkpoint remains
  **969 passed, 0 failed, 3 PostgreSQL-dependent skipped**; this unit has not yet
  rerun the full suite. Frontend unchanged: latest five component tests/build pass.
- BT-001 is partial, not complete. Counts: **206 tested / 13 implemented-unverified
  / 84 partial / 230 not started**. Full-day coverage, child-process isolation,
  PostgreSQL execution, external feeds, Groww LIVE and M1/M2 remain unverified.

### Exact next high-value unit

Add the dedicated historical child-process entry point/launcher before API/UI
exposure. Never change the serving application's global clock/database/gates.
Require explicit manifest/recording/settings files and an already-migrated empty
dedicated database; preserve sanitized failures and nonzero incomplete outcomes.
Then verify full-session coverage/fill conventions and run broader regression
before metrics/OOS/walk-forward work.

## Historical child-process launcher

- Added an actual separate-process launcher with a restricted environment,
  temporary working directory, explicit manifest/recording/settings paths,
  bounded runtime and no shell execution. Parent database, clock, gates and
  broker credentials are not inherited. Windows children remain hidden.
- The child is fixed to PAPER and uses the same prepared-run implementation.
  Configuration installation remains centralized in `app/config.py`; no
  environment-access exception was added to the architecture tests. Console
  failures expose only exception classes, not secrets or payloads.
- Actual process tests cover completed and incomplete real recorded trades,
  conflicting parent configuration, unchanged parent state, reused-database
  refusal and sanitized invalid input. Focused subprocess/layering verification:
  **10 passed, 0 failed, 0 skipped**. New modules pass Ruff; existing config
  upgrade/style findings remain unchanged (UP035/UP037/UP045/RUF022).
- The first broad regression was interrupted and its process confirmed absent;
  its partial log is not a passing result. Replacement full backend/browser
  regression: **979 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including
  all three actual browser lifecycle scenarios. Two existing dependency warnings
  remain. Frontend: **5 passed, 0 failed**, production build successful.
- Counts remain **206 tested / 13 implemented-unverified / 84 partial / 230 not
  started**. BT-001 remains partial. Timeout/OS termination may leave RUNNING;
  this is interrupted/unverified, never success. PostgreSQL, Groww LIVE and
  external feed/notification delivery remain unverified.

### Exact next high-value unit

Implement recorded full-session coverage/expected warm-up accounting and fill-convention checks,
before real backtest metrics and API/UI launch controls. Preserve source
stand-downs and missing-data failure evidence rather than labelling incomplete
market coverage successful.

## Stored full-session PAPER regression

- A saved/hash-verified deterministic recording now traverses the shared worker
  from 09:15 market open to 15:40 market close, including entry blackout,
  intraday analysis, no-entry window, square-off window and EOD reconciliation.
  One-minute candle/depth/breadth/IV fixtures produce one actual strategy trade,
  risk/preflight-approved entry, protection, target exit and net journal result.
  The fixed reference is gross 888, charges 31.44, net 856.56; these are isolated
  fixture expectations, not market returns or a profitability claim.
- A removed quote creates stale evidence: ingestion audits rejection, the
  existing worker error latch fails closed before the signal, no order occurs,
  and the run stops FAILED. The initial test incorrectly expected recovery/
  INCOMPLETE; that expectation was corrected rather than weakening the latch.
- Both stored-session scenarios pass: **2 passed, 0 failed, 0 skipped**. Ruff
  passes. Production code is unchanged since the full **979 passed / 0 failed /
  3 PostgreSQL-dependent skipped** checkpoint. Frontend five tests/build pass.
- PAPER-008 is integration-tested. Counts: **207 tested / 13 implemented-unverified
  / 83 partial / 230 not started**. The session uses explicit 60-second monitoring
  and a 30-minute entry blackout to collect declared warm-up inputs; it does not
  claim tick-continuous coverage or every recording's completeness. No external
  feed, PostgreSQL, Groww LIVE, notification delivery or M1/M2 claim is made.

### Exact next high-value unit

Persist actual sampled equity/exposure and linked trade outcomes per historical
run, then connect shared net-performance metrics and real API/UI run inspection.
Keep undefined annualized metrics unavailable on inadequate samples; never
manufacture returns between missing observations. Full backtest parity, alternate
fill conventions, OOS/walk-forward and the remaining production ledger still
require implementation/verification.

## Historical account samples and linked trade persistence

- Historical runs now persist a baseline and post-cycle observations in their
  audit chain: actual shared-account net equity/cash, gross realised P&L, charges,
  unrealised P&L, exposure/margin and open-position identities. Missing costs,
  stale marks and failed-worker state produce explicit unavailable values.
- Final results retain an equity curve with audit-linked observation metadata;
  closed journal entries populate backtest trades with journal/proposal/risk/
  order/position/audit linkage. Open positions are never manufactured as closed
  trades, and no missing equity point is interpolated or forward-filled.
- Tests caught initial incorrect sample placement (only the baseline persisted);
  sampling now runs inside each actual timeline cycle. Focused historical engine/
  child-process/bootstrap verification: **15 passed, 0 failed, 0 skipped**.
  Stored full-session success/gap regression: **2 passed, 0 failed, 0 skipped**,
  including 387 real cycle/baseline samples and an unavailable failed-cycle point.
  Ruff passes. Last full backend/browser result remains **979 passed / 0 failed /
  3 skipped**, before this unit; frontend source/schema unchanged, five tests/build pass.
- BT-007 is partial: drawdown, shared metrics and API/UI inspection remain pending.
  Counts: **207 tested / 13 implemented-unverified / 84 partial / 229 not started**.
  PostgreSQL, Groww LIVE, external data/notification delivery and M1/M2 remain
  unverified. Fixture net outcomes are not performance claims.

### Exact next high-value unit

Implement shared performance metrics from the persisted net-equity/trade
observations, with explicit undefined/insufficient-data handling and independent
numeric references. Wire results to authenticated historical-run read APIs and
the dashboard, without connecting the serving API to a historical process clock
or an arbitrary client-supplied database. OOS/walk-forward and remaining ledger
requirements follow genuine report integration.

## Shared descriptive net metrics

- Historical results now use the shared portfolio metrics implementation for
  total return, sampled drawdown, recovery factor, win rate, average win/loss,
  profit factor, expectancy and average holding time. Drawdown curves and exact
  Decimal metric strings are persisted with the actual account/journal evidence.
- Independent hand calculations cover gains/losses/breakevens, insolvency,
  missing equity or net costs, malformed observations and zero denominators.
  Undefined values remain unavailable rather than zero or infinity. Irregular
  event/cadence samples do not fabricate CAGR/Sharpe/Sortino or exposure duration.
- Focused metrics/engine/actual subprocess regression: **16 passed, 0 failed,
  0 skipped**. Tests also verify existing six-decimal database ratio rounding
  against unrounded Decimal metadata. Ruff passes. Full backend/browser suite:
  **989 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including stored
  full-session/gap and actual browser tests. Two existing dependency warnings remain. Frontend
  source/schema unchanged, latest five tests/build pass.
- PNL-006 and BT-006 are partial, not complete; BT-007 still needs API/UI wiring.
  Counts: **207 tested / 13 implemented-unverified / 86 partial / 227 not started**.
  All external verification limitations and PAPER-only execution remain intact.

### Exact next high-value unit

Implement authenticated
historical run/result read APIs with typed schemas and truthful empty/incomplete/
unavailable states, then connect real dashboard inspection. Do not allow client
database URLs or load a historical clock into the serving application. Controlled
cross-database result publication/launch and annualized/OOS/walk-forward metrics
remain separate pending work.

## Authenticated historical API/dashboard inspection

- Added authenticated typed run list/detail, trade and account-sample GET APIs
  with bounded pagination, actual status/progress, net metrics, curve points and
  unavailable values. Only the configured database is queried; no client database
  URL, raw configuration, launch command or historical-clock mutation is accepted.
- The API-backed Backtests view polls real state, displays incomplete/failed/
  empty/error states, charts equity/drawdown without filling gaps, and lists
  actual closed trades. No launch action is offered. New types are generated
  from the actual API schema, not separately invented frontend contracts.
- Focused API/authentication tests: **7 passed**. Frontend: **7 passed**, production
  build successful. All **4 actual browser scenarios pass**, including a real
  recorded run viewed through authentication and React. The browser test caught
  a wrapped selector-label defect missed by DOM tests; the label now explicitly
  targets its select element. Existing browser server infrastructure is shared.
- Full backend/browser regression: **992 passed, 0 failed, 3 PostgreSQL-dependent
  skipped**; two existing dependency warnings remain. FE-013 and BT-007 remain partial: isolated run databases
  are not automatically published into the normal PAPER application's database.
  Counts: **207 tested / 13 implemented-unverified / 87 partial / 226 not started**.
  External feeds/Groww LIVE/notifications/PostgreSQL and M1/M2 remain unverified.

### Exact next high-value unit

Implement controlled, audited publication of isolated historical results into
the normal application catalog, using server-owned configuration and explicit
run identity/provenance checks. Never accept arbitrary client database targets
or import historical account state into the live/PAPER account. Connect actual
launch/progress control only after this isolation boundary is verified.

## Audit-bound historical catalog publication

- Newly finalized historical runs bind stored run/result/trade columns to a
  canonical digest in their historical audit chain. Publication validates that
  binding, expected recording identity, source database name, simulated status
  and terminal outcome before opening a destination write transaction.
- An owner-operated local CLI publishes only catalog/report audit rows into the
  configured non-historical application database. Orders, positions, fills,
  journals, risk/latch/account state, authentication and market tables are never
  imported. Source lineage remains in its original database, which must be kept.
- Identical republishing is idempotent after destination integrity checks.
  Tampered results/audits, wrong recordings, unfinished runs and identity
  conflicts fail closed. Publication-audit failure rolls back all inserted rows.
  Legacy finalized runs without a digest are refused, not retroactively certified.
- Focused publication/engine/child-process verification: **14 passed, 0 failed,
  0 skipped**, including an actual CLI process while a separate PAPER account has
  an open position; every non-catalog table and its safety gates remain unchanged.
  The real historical browser scenario passes after cross-database publication
  and page reload. A Windows subprocess sandbox denial required the approved
  elevated test run, not a code bypass. Ruff passes.
- Full backend/browser regression: **998 passed, 0 failed, 3 PostgreSQL-dependent
  skipped**, with two existing dependency warnings. Frontend production code is
  unchanged since seven tests/build passed. BT-007 is integrated and tested;
  FE-013 remains partial because API/UI configuration/launch is not implemented.
  Counts: **208 tested / 13 implemented-unverified / 86 partial / 226 not started**.
  PostgreSQL, external feeds/Groww LIVE/notification delivery and M1/M2 remain
  unverified. Catalog hashes are not independent market/performance validation.

### Exact next high-value unit

Add server-owned
historical job configuration and authenticated launch/progress controls over the
existing child-process runner and publication service. Bound job concurrency,
refuse arbitrary client database/file paths, preserve restart/interruption
outcomes and audit owner actions before enabling launch from the dashboard.

## Authenticated isolated historical jobs

- Server-owned bounded plan registry, digest/identity validation and immutable
  staged inputs connect authenticated owner requests to the existing actual
  child runner and audited catalog publisher. No client database/path fields,
  serving-process clock replacement or account-state import is permitted.
- Migration `0008_historical_jobs` adds a durable unique execution reservation.
  Owner requests and status changes are audited. Repeated plan identities are
  idempotent; separate controllers cannot reserve a second active slot.
- Shutdown/timeout reaps the owned process before release. Abrupt-restart rows
  stay reserved and explicitly UNVERIFIED_OWNER_REVIEW_REQUIRED; no unsafe
  automatic retry/slot clearing. Authenticated orphan recovery remains pending.
- Three authenticated job API operations and the existing Backtests view expose
  owner plan selection, typed confirmation, real progress, publication state,
  unavailable services and unverified controller state. Polling uses actual API
  records, never synthetic production responses.
- Focused job verification: **7 passed, 0 failed, 0 skipped** covering an actual
  child run/publication, duplicate request, restart reservation, shutdown,
  source-schema failure, real timeout, path refusal and audit-write rollback.
  The actual browser launches from an empty catalog, observes the published
  costed trade/report and reloads it. Frontend **8 passed**, production build
  successful. A browser selector defect was caught and corrected with an
  explicit accessible label; no API fixture was substituted.
- Full backend/browser regression: **1005 passed, 0 failed, 3 PostgreSQL-dependent
  skipped**, including all four real-browser scenarios; two existing dependency
  deprecation warnings. Ruff passes for new/changed historical modules and tests;
  Alembic resolves one head, `0008_historical_jobs` (not PostgreSQL execution proof).
  FE-013 remains partial (server-owned configuration only; no browser parameter
  editing or orphan recovery). Counts unchanged: **208 tested / 13 unverified /
  86 partial / 226 not started**. PostgreSQL migration execution, Groww LIVE,
  external feeds/delivery and M1/M2 remain unverified.

### Exact next high-value unit

Verify historical reproducibility, risk-limit enforcement and no-look-ahead
through the actual isolated runner, using adversarial recordings and independently
expected outcomes. Reuse the shared worker and report/accounting services; do not
create a second backtest pipeline. Then build explicit OOS/walk-forward evidence.

## Historical reproducibility and adversarial risk/cost evidence

- Final historical audit events now contain versioned input/outcome/metric/trade
  economic fingerprints. The existing typed detail API and dashboard display
  these persisted values; legacy and unfinished runs show UNAVAILABLE. Original
  lineage remains intact, and catalog publication retains its complete integrity
  binding. Fingerprints are not external verification or profitability claims.
- Actual isolated processes replay identical inputs into separate empty
  databases with identical economic results. An extreme future candle changes
  the input hash but cannot change earlier output. Tight instrument concentration
  limits persist a deterministic risk-engine rejection and create no orders.
  Increasing an explicit test stamp rate changes actual charges by the independently
  computed amount while gross trade P&L remains unchanged. These are isolated
  synthetic regression fixtures, not real-market performance.
- Focused acceptance/API/publication suite: **10 passed, 0 failed, 0 skipped**.
  Frontend **8 passed**, production build successful. Full backend/browser suite:
  **1007 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including all four
  real-browser scenarios and persisted fingerprint display after publication.
  Two existing dependency deprecation warnings remain. Ruff passes.
- BT-005 and BT-008 acceptance is implemented with integrated evidence. BT-002
  and BT-004 remain partial: malicious-strategy acceptance and broader product
  transaction costs are not complete. Ledger: **210 tested / 13 unverified /
  88 partial / 222 not started**. No M1/M2, Groww LIVE, external feed/delivery or
  PostgreSQL verification claim is made.

### Exact next high-value unit

Implement explicit chronological OOS/walk-forward experiment orchestration over
the existing isolated historical runner: non-overlapping decision windows,
frozen strategy/parameter selection using in-sample evidence only, separate OOS
results and audit-linked reports. Never pool training trades into OOS metrics or
infer profitability/validation from synthetic regression fixtures.

## Chronological OOS/walk-forward application path

- The existing owner plan registry now supports bounded experiments with UTC
  half-open training/test windows, optional explicit embargo, fixed candidate
  groups and no overlapping OOS decision windows. Child inputs are validated and
  hash-frozen before execution; reused IDs and contradictory historical
  observations across source files are refused. No capital/data is invented.
- The actual historical job controller executes every training candidate through
  the shared worker. Only typed, completed training net-return scores enter
  selection. A committed audit freezes the chosen candidate before its OOS child
  can start. Failed input checks, insufficient training evidence and selection
  audit failure prevent OOS execution. Unselected OOS candidates are not run.
- OOS-only trades, per-window results and nominal aggregate costs/P&L appear in
  the existing Backtests view through authenticated APIs. Training gains are not
  mixed into OOS results. Accounts reset between windows: continuous portfolio
  returns/drawdown and aggregate equity curves remain explicitly UNAVAILABLE.
  This supports the reference strategy's declared risk-fraction candidates, not
  arbitrary strategy-family optimisation or LIVE approval.
- Migration `0009_walkforward_jobs` reserves one durable experiment. The existing
  historical slot bounds child concurrency. Graceful stop reaps the actual child;
  restart leaves unknown ownership reserved and visible as
  UNVERIFIED_OWNER_REVIEW_REQUIRED. No automatic retry or orphan-slot clearing.
- Focused final experiment/recovery suite: **10 passed, 0 failed, 0 skipped**.
  Earlier historical/API compatibility suite: **16 passed**. Real browser launch
  and reload verified OOS losses, not positive training P&L, alongside the existing
  historical launch scenario. Frontend **8 passed**, production build successful;
  Ruff passes and Alembic resolves one head. Full backend/browser regression:
  **1018 passed, 0 failed, 3 PostgreSQL-dependent skipped**, including all five
  real-browser scenarios; two existing dependency deprecation warnings remain.
  The first full run lost its tool host and left only a partial log. After the
  original process and session handle were confirmed absent, verification was
  restarted and completed successfully; the interrupted run is not counted as
  a passing result. PostgreSQL execution of the new migration is still unverified.
- Verification caught invalid minute-shifted fixture candles at the regime gate;
  the fixture was corrected without weakening production validation. Review also
  found contradictory overlapping warm-up observations between independent
  fixture tapes. Cross-file consistency validation now rejects those conflicts;
  the regression uses explicit embargo and non-conflicting data windows.
- WF-001/002 have integrated acceptance evidence; WF-003 remains partial pending
  multi-window aggregate/drift verification. Counts: **212 tested / 13 unverified /
  89 partial / 219 not started**. PostgreSQL migration execution, external data,
  Groww LIVE, delivery verification, statistical OOS validation and M1/M2 remain
  unverified. The losing synthetic OOS fixture is not real-market evidence.

### Exact next high-value unit

Complete and verify multi-window OOS aggregation, parameter drift/degradation
reporting and parent-report audit binding/lineage in the same application path.
Keep independent reset-account results distinct from continuous-portfolio
metrics. Then implement explicit strategy validation thresholds; do not enable
LIVE or substitute synthetic fixtures for time-based PAPER/OOS evidence.

## Verified multi-window report checkpoint (2026-09-23)

- Added explicit owner-configured IS/OOS return-drop diagnostics, parameter drift,
  per-window stability and original trade/run lineage to typed historical APIs.
  The existing Backtests view renders real diagnostics and internal integrity.
- Completed reports now atomically bind their aggregate and terminal status to
  the audit chain. Reads reject modified report contents. Finalization checks
  per-window trade counts, time bounds and gross/charges/net reconciliation.
- Focused report/framework verification: **10 passed**; report/failure/recovery
  verification: **6 passed**. Final audit failure and inconsistent accounting
  cannot publish a completed aggregate. A real two-window browser scenario
  passed. Frontend **8 passed**, production build successful.
- Full backend/browser regression: **1022 passed / 0 failed / 3 skipped**, including
  all five real browser scenarios. Skips require PostgreSQL; two existing
  dependency deprecation warnings remain. WF-003/005 now have integrated
  acceptance evidence. Counts: **214 tested / 13 unverified / 88 partial /
  218 not started** (533 total). No external verification or M1/M2 claim.
- Independent reset-capital windows do not create a continuous portfolio return
  or drawdown. Missing degradation thresholds remain UNAVAILABLE. Synthetic
  fixtures are not external market validation or time-based PAPER evidence.
- Exact next task: implement explicit strategy-validation thresholds and evidence
  gates against persisted reports, with authenticated/audited API exposure.
  Do not enable LIVE or grant approval from synthetic history. Verified external
  data, time-based PAPER evidence and PostgreSQL execution remain unavailable.

## Verified predeclared evidence review checkpoint (2026-09-23)

- Optional OOS thresholds are now part of the owner experiment specification,
  frozen before execution. Checks cover trade/window counts, net expectancy,
  maximum within-window drawdown and profitable-window consistency. No default
  policy is invented, and absent/nonfinite metrics fail closed.
- Authenticated `POST /api/v1/backtests/{id}/review` accepts a reason/confirmation,
  not client-supplied metrics or thresholds. It checks report integrity and audits
  the report digest, policy, outcome and actor. The existing Backtests view exposes
  checks and records reviews. Read polling does not create audit events.
- A passing research review never sets strategy approval, enablement or LIVE
  permissions. Exact strategy-version evidence binding, externally verified
  backtest inputs and time-based PAPER evidence remain explicit blockers.
- Focused framework/review/recovery tests: **21 passed**. Updated real-browser
  review scenario: **1 passed**. Frontend **8 passed**, production build successful.
  Full backend/browser regression, including final review audit-failure rollback:
  **1029 passed / 0 failed / 3 PostgreSQL-dependent skipped**, including all five
  real browser scenarios. Two existing dependency deprecation warnings remain.
- STRAT-010 and WF-004 are honestly partial, not complete: integrated research
  reviews are not a strategy approval service. Counts: **214 tested / 13 unverified /
  90 partial / 216 not started**, total 533. No M1/M2, external-data, notification
  delivery, PostgreSQL or Groww LIVE verification is claimed.
- Exact next task after verification: bind evidence to immutable strategy
  versions/parameters and implement provenance-filtered time-based PAPER session
  and trade eligibility. Mixed-parameter walk-forward aggregates and synthetic
  fixtures must never approve a fixed strategy for LIVE.

## Verified immutable strategy report binding (2026-09-23)

- Historical bootstrap now persists the actual registered strategy specification
  and canonical parameter hash before execution. These travel through report
  publication, final audit digests and economic reproducibility fingerprints.
- OOS window diagnostics retain source bindings. Aggregate API/dashboard state
  distinguishes FIXED, MIXED and UNAVAILABLE. Different isolated risk-fraction
  candidates can share a version label; they must not be conflated into approval
  evidence for one immutable strategy. Legacy missing bindings are not backfilled
  from today's code, and altered specification/hash pairs are rejected.
- Broad historical/bootstrap/engine/publication/reproducibility/walk-forward and
  evidence tests: **61 passed / 0 failed / 2 opt-in browser skipped**. Both skipped
  scenarios were then run with the real browser: **2 passed / 0 failed**. Frontend
  **8 passed**, production build successful, Ruff and whitespace checks pass.
  Latest full-suite baseline remains **1029 passed / 0 failed / 3 PostgreSQL skipped**
  at `c6d77fe`; that is not relabelled as a full run of this metadata extension.
- No requirement is newly complete. Counts remain **214 tested / 13 unverified /
  90 partial / 216 not started**. PAPER remains default; Groww LIVE and M1/M2 remain
  unverified. A fixed binding still confers no trading permission.
- Exact next task: implement provenance-filtered PAPER session/trade eligibility
  and immutable registration-to-report evidence linkage in the authenticated
  strategy review path. Exclude REPLAY/HISTORICAL/SYNTHETIC, future timestamps,
  uncosted or unaudited trades, and mismatched parameter hashes. Count only
  genuinely recorded session coverage, not merely dates inferred from trades.

## Verified PAPER evidence integration checkpoint (2026-09-23)

- Added owner-declared, audit-versioned coverage/session/trade thresholds. The
  existing worker records opt-in observations with live-origin quote provenance,
  strategy hashes, session bounds, health and worker identities. Restart gaps are
  not credited, and a post-close flat observation is required.
- Closed PAPER journals now receive a digest atomically with local lifecycle
  updates. Journal audit failure rolls back local closure; recovery reconstructs
  the actual simulated fill once, without placing another order.
- Added authenticated strategy policy/read/review APIs and controls inside the
  existing Strategies view. Reviews match report digests and immutable strategy
  hashes, reject mixed-parameter OOS as fixed-strategy evidence, and keep LIVE
  disabled. Live-labelled isolated test fixtures do not establish external proof.
- Focused evidence suite: **6 passed**; initial execution/reference regression:
  **19 passed**; initial report/linkage subset: **7 passed**. Frontend **8 passed**,
  production build successful. Final backend regression: **1036 passed / 0 failed /
  3 PostgreSQL-dependent skipped**, including all five actual browser scenarios.
  Two existing dependency deprecation warnings remain. Ruff and whitespace checks
  pass; no migration was added in this unit.
- Verification fixed a selector accessibility defect and demonstrated audit-time
  advancement after sample observation. Production collection now checks quote
  expiry after collection and accepts bounded later audit timestamps, rather than
  requiring equality that only a frozen test clock could satisfy.
- PAPER-006 advances to partial, not complete; actual time-based validation,
  external provenance attestation and LIVE approval/arming remain unavailable.
  Counts: **214 tested / 13 unverified / 91 partial / 215 not started**, total 533.
  No M1/M2, Groww LIVE, notification delivery or PostgreSQL validation is claimed.
- Remaining scope: actual external/time-based evidence, approval/arming lifecycle,
  journal correction/annotation/query/export and other contract gaps. The next
  independent implementation should close the real journal workflow gaps rather
  than treating local eligibility as LIVE permission. Exact next task: route
  actual shared-pipeline rejections into the journal, add authenticated typed
  journal list/detail/lineage and annotation/export APIs, and connect the existing
  Journal view. Preserve immutable economic fields and implement audited versioned
  corrections instead of allowing updates to a sealed trade record.

## Verified integrated journal workflow (2026-09-23)

- Shared-pipeline validation/sizing/risk negatives now create transactional,
  audit-bound REJECTION journals with actual context and reasons, never invented
  fills or P&L. Costed PAPER trade journals retain their existing seal and now
  include recorded exit duration, slippage, risk and net outcome metrics.
- Added authenticated typed journal list/filter/detail/annotation and bounded
  CSV/JSON export APIs. Detailed lineage returns actual proposal, sizing, risk,
  orders, fills, exits, position, regime and audit records; missing/legacy evidence
  is explicitly incomplete. Audited annotation deletion/modification and core
  digest mismatches fail closed. Annotation audit failure rolls back the note.
- Connected the existing Journal view with local-day filters, pagination,
  ten-second refresh, lineage inspection, owner notes/tags and authenticated
  downloads. No new page or hardcoded market/performance values. CSV JSON-value
  cells preserve every column and nested field while neutralizing formulas.
- New journal integration coverage: **9 passed**. Focused pipeline/reference/
  execution/journal regression passed. Real PAPER browser scenarios: **3 passed**,
  including actual lineage, annotation and downloaded JSON artifact verification.
  Full backend regression: **1045 passed / 0 failed / 3 PostgreSQL-dependent
  skipped**, including all five browser scenarios, in 558.71 seconds. Two existing
  dependency deprecation warnings remain. Frontend **10 passed / 0 failed**;
  production build, targeted Ruff and whitespace checks pass.
- FE-012 and JRN-004/005/006 meet their integrated acceptance criteria. Counts:
  **218 tested / 13 unverified / 90 partial / 212 not started**, total 533.
  General cross-mode journal completeness/context/exit coverage remain partial;
  no M1/M2, PostgreSQL, external notifications, data-provider or Groww LIVE
  verification is claimed. PAPER remains the default.
- Exact next task: JRN-007 immutable core journal enforcement and audited
  versioned corrections. Preserve original economic evidence and audit seals;
  corrections must not double-count trades or become unverified PAPER eligibility
  evidence. Add authenticated version lineage without allowing in-place economic
  edits, with rollback/concurrency/tamper tests and honest database limitations.

## Integrated immutable journal revisions (2026-09-24)

- Added append-only ORM attribute/delete/bulk guards and migration
  `0010_journal_revisions` for durable version links and journal/annotation/link
  UPDATE/DELETE refusal. SQLite upgrade, raw-SQL refusal and downgrade are tested;
  PostgreSQL trigger execution remains externally unverified.
- Owner corrections append a sealed contextual version with expected-version
  concurrency control, actor/reason and atomic root-chain audit. Originals,
  economics, broker fills and source evidence are never rewritten. Only plan
  adherence assessment or rejection explanation is correctable; arbitrary financial
  edits are explicitly rejected. Original annotations remain on their version.
- Integrated version/original navigation in the existing Journal view and version
  metadata in exports. Workspace/historical economic totals use original records,
  and corrected originals/revisions cannot inflate PAPER eligibility. Revision
  deletion/tampering is detected even when simulated outside ORM safeguards.
- Focused revision tests: **6 passed**. Journal/migration/schema regression:
  **45 passed / 0 failed / 3 PostgreSQL skips**. Actual PAPER browser scenarios:
  **3 passed**, including contextual revision creation and unchanged-original
  inspection. Frontend **10 passed**, production build successful. Browser
  verification caught and fixed a select-label accessibility issue.
- The initial full regression process disappeared without a terminal summary;
  its partial log is not passing evidence. The fresh full run completed:
  **1051 passed / 0 failed / 3 PostgreSQL-dependent skipped** in 700.30 seconds,
  including all five real browser scenarios. Evidence is in
  `backend/logs/journal-revisions-verification-retry.txt`. Two existing dependency
  deprecation warnings remain. Targeted Ruff and whitespace checks pass; existing
  model-module Optional/forward-reference modernization warnings are unchanged.
- JRN-007's integrated immutable/versioned-record acceptance is covered. Counts:
  **219 tested / 13 unverified / 90 partial / 211 not started**, total 533.
  No M1/M2, external provider, notification delivery, PostgreSQL or Groww LIVE
  verification is claimed. PAPER remains the default.
- Next highest-value implementation after verification/commit: close intraday
  entry-safety gaps INTRA-003/004/005 in the existing decision/execution path.
  Existing configured daily-count and loss-cooloff policies must be enforced from
  durable actual history at proposal and execution preflight, not merely scheduler
  state. Verify restart, duplicate/partial fills, time boundaries, stale/missing
  evidence and product-contract refusal; expose actual blocked decisions in the
  existing APIs/UI without adding a parallel execution path.

## Verified product-contract execution hardening (2026-09-24)

- Inspection found a real bypass: execution checked strategy enablement, but a
  modified persisted proposal/order could change MIS to CNC without matching the
  registered strategy. Preflight and dispatch now bind the product and complete
  strategy snapshot to the immutable registered specification/hash. The final
  dispatch also checks order product against the approved proposal.
- Shared product policy rejects CNC outside CASH and NRML outside FNO, and honors
  the existing owner `ALLOW_POSITIONAL=false` default. Quantitative signals cannot
  override their declared product; rejected pipeline candidates appear in the
  existing journal/API. Permission withdrawn between proposal and submission blocks
  the broker call. Historical execution settings now freeze this permission.
- Product integration tests: **5 passed**; contract/configuration unit regression:
  **39 passed**; broader pipeline/strategy/PAPER execution/reference worker/historical
  bootstrap/engine regression: **77 passed / 0 failed / 0 skipped**. Targeted Ruff
  and whitespace checks pass. No frontend source changed in this unit. Last full
  backend/browser baseline remains **1051 passed / 0 failed / 3 PostgreSQL skips**
  at `12502bc`; this targeted regression is not relabelled as a new full run.
- INTRA-005 meets its declared-product acceptance. Counts: **220 tested / 13
  unverified / 90 partial / 210 not started**, total 533. General F&O execution,
  SUPERVISED/LIVE and external verification remain pending; an allowed product does
  not bypass other gates. PAPER remains default and Groww LIVE remains unverified.
- Exact next task: INTRA-003/004 durable daily entry-count and post-loss cooldown
  enforcement in both shared decisions and execution preflight/dispatch. Preserve
  risk audit replay and origin/time boundaries; count order intents once across
  partial fills/retries, conservatively reserve unknown/pending entries, and
  derive losses only from original recorded economics. The scheduler currently
  enforces a count cap but direct execution still needs the authoritative check.

## Verified durable entry policy and exact FIFO valuation (2026-09-24)

- INTRA-003/004 now use authoritative persisted entry intents, fills and original
  sealed trade journals in shared decisions, PAPER preflight and final dispatch.
  Count is account-wide across strategies within execution mode/data origin, on
  the IST trading date. Pending/unknown intents reserve quota conservatively;
  partial fills count once and zero-filled confirmed cancellations release quota.
  Same-instrument net losses enforce the configured cooldown; unknown net costs,
  missing journals, invalid provenance and future mutable history fail closed.
- Frozen policy/evidence and checks are persisted in risk snapshots and replayed
  without consulting today's settings/history. Existing workspace/preflight API
  exposes typed evidence; rejected candidates retain journal/audit lineage. Final
  dispatch rechecks current history and records its own audited check. The worker
  no longer hides this decision behind a scheduler-only count gate.
- An adversarial split fill exposed a real rounding defect: local four-decimal
  display averages differed from exact broker FIFO averages. Reconciliation now
  compares exact remaining fill lots and permits only display-storage rounding;
  mark-to-market uses actual fill lots instead of the rounded display average.
- Full backend suite including five real browser scenarios: **1067 passed / 0
  failed / 3 PostgreSQL-dependent skipped**, 632.30 seconds. Evidence:
  `backend/logs/entry-policy-verification.txt`. Frontend: **10 passed**, production
  build successful; OpenAPI and TypeScript response schemas regenerated. Two
  existing dependency deprecation warnings remain. No external verification is
  implied by isolated fixture-driven lifecycle tests.
- Counts: **222 tested / 13 implemented-unverified / 89 partial / 209 not started**,
  total 533. PAPER remains default; Groww LIVE, PostgreSQL runtime and M1/M2 remain
  unverified. No push performed; owner working-tree edits are preserved.
- Exact next task: enforce INTRA-002 no-entry windows independently in shared
  proposal decisions and final PAPER preflight/dispatch, not solely the worker's
  in-memory session gate. Reuse the exchange calendar/session boundaries and
  configured open/close blackout/square-off settings; test exact boundaries,
  special sessions, configuration validation, disabled-worker direct entry and
  persisted rejection/API evidence. Exits must remain available during blackouts.

## Verified shared entry windows (2026-09-27)

- INTRA-002 closes the disabled-worker/direct-entry bypass: shared proposal risk,
  PAPER preflight and final dispatch independently enforce the same calendar-based
  windows as the worker. Evidence includes source, session/entry boundaries and
  square-off cutoff in the replayable risk snapshot and typed workspace response.
  Blackout policy changes between approval and dispatch block the broker call.
- Negative blackout durations and non-HH:MM square-off values are rejected.
  Incomplete calendar coverage fails closed. Special sessions retain their own
  bounds; overlapping blackouts cannot create an accidental trading interval or
  mislabel post-close time. Exits remain available during entry blackouts.
  Explicit synthetic test calendars stay inside fixtures; the production calendar
  is not relabelled complete. Historical runs use their manifest calendar.
- Verification across all collected tests in disjoint completed batches:
  **1073 passed / 0 failed / 3 PostgreSQL-dependent skipped**. Unit: 628;
  integration batches: 189 (+3 skips), 91, 25, 32, 45, 58, and all 5 real browser
  scenarios. The 25-test batch covers PAPER costs/evidence/exchange/execution;
  other logs are `backend/logs/entry-window-unit.txt`, `entry-window-integration-a.txt`,
  `entry-window-integration-b1.txt`, `entry-window-integration-b3b.txt`,
  `entry-window-integration-c1.txt`, `entry-window-integration-c2.txt`, and
  `entry-window-browser-complete.txt`. Source was unchanged across these batches.
- Earlier monolithic runs were interrupted by the test host. One recorded a browser
  failure marker without a terminal traceback; that scenario passed in isolation
  and then in the completed five-browser batch. Those interrupted logs are retained
  and are not counted as passing runs. Existing dependency deprecation warnings
  remain. Frontend: **10 passed**, production build successful, regenerated API types.
- Counts: **223 tested / 13 implemented-unverified / 88 partial / 209 not started**,
  total 533. No new pages or fake runtime values. PAPER remains default; external
  delivery/providers, PostgreSQL runtime, Groww LIVE and M1/M2 remain unverified.
- Exact next task: complete the actual PAPER failure-to-notification path for
  reconciliation discrepancies, feed outages and worker failures using the existing
  durable audit/outbox. Preserve fail-closed execution, persist notifications before
  errors roll back caller transactions, deduplicate repeated incidents across restart,
  and expose real pending/failed delivery state through existing Monitoring APIs/UI.
  Keep NOTIF-003 partial until every mandatory producer has integration evidence;
  do not claim remote delivery without authorized external verification.

## Durable PAPER failure notifications (2026-09-27)

- Reconciliation exceptions now produce a committed CRITICAL incident/outbox
  request outside the failed transaction. Audited active/recovered transitions
  deduplicate repeated observations across executor restart; a genuine recurrence
  receives a new notification. Invalid audit history and outbox persistence errors
  fail closed. No notification delivery attempt runs inside the trading caller.
- Worker failures and stale/invalid/missing monitored quotes produce durable
  CRITICAL notices after the failed heartbeat is committed. Authenticated worker
  review resolves worker/feed incidents atomically with its existing review audit.
  Reconciliation recovery clears no risk/emergency controls; notification audit
  recovery clears only its own availability gate after a successful transaction.
- Tests exposed suppression of a distinct recurrence sharing the same message.
  Transition messages now include their persisted audit identity, preserving both
  durable incident deduplication and delivery of distinct active/recovered events.
- Targeted production-path regression: **43 passed / 0 failed / 0 skipped**
  (`backend/logs/paper-incidents-verification.txt`), including actual mismatched
  positions, rollback, restart, repeated stale data, owner review, costed lifecycle,
  outbox delivery fixtures and workspace visibility. Additional notification-unit
  and historical bootstrap/engine regression: **34 passed / 0 failed / 0 skipped**.
  Targeted Ruff and whitespace checks pass.
- Last complete suite coverage remains **1073 passed / 3 PostgreSQL skips** in
  completed batches at `cbae1ce`; the narrower regression above is not labelled a
  new full run. Frontend/API schema unchanged; last frontend verification remains
  **10 passed + successful build**. No new page or external delivery claim.
- NOTIF-003 remains partial. Counts unchanged: **223 tested / 13 unverified / 88
  partial / 209 not started**. PAPER remains default and Groww LIVE unverified.
- Exact next task: move daily-loss/drawdown/engine-error latch notifications from
  the in-memory emitter into the existing transactional audit/outbox path, preserving
  latch persistence, explicit reset behavior and remote-delivery failure isolation.
  Then complete provider-level feed/auth and LLM-budget producer coverage wherever
  the underlying producer genuinely exists; keep missing integrations partial.

## Transactional risk-latch notifications (2026-09-27)

- Daily-loss, drawdown and engine-error breaches now commit CRITICAL outbox requests
  with their risk audit, rather than relying on a post-commit in-memory emitter.
  Disabled delivery preserves pending requests across restart. Re-observation of
  a latched condition does not create duplicate requests, and remote failures cannot
  re-arm trading. Database/outbox failure rolls back the transition and blocks
  entries through the existing risk-storage gate; storage-unavailable reporting
  remains explicitly best-effort logging/in-memory fallback, not fake persistence.
- Found and fixed a daily-boundary defect: if the first observation on a new IST
  day was already below the daily limit, yesterday's latched flag suppressed the
  new day's critical event. Daily transitions now include session identity; an
  adversarial rollover test proves a new alert once, not on every subsequent tick.
- Risk/control/outbox/PAPER integration regression: **47 passed / 0 failed / 0
  skipped** (`backend/logs/risk-outbox-verification.txt`). All unit tests: **628
  passed** (`risk-outbox-unit.txt`). All five real browser scenarios pass against
  the built dashboard (`risk-outbox-browser.txt`), including actual costed trade,
  emergency and stale-emergency behavior, historical results and walk-forward/OOS.
  Targeted Ruff passes. Frontend source and API schemas did not change.
- Last complete suite coverage remains **1073 passed / 3 PostgreSQL skips** at
  `cbae1ce`; these newer targeted results do not constitute a new full-suite count.
  NOTIF-003 remains partial. Counts: **223 tested / 13 unverified / 88 partial / 209
  not started**. No external delivery, real market feed, Groww LIVE or M1/M2 claim.
- Exact next task: connect provider feed/auth failure transitions from actual health
  checks and reference ingestion to typed durable notices, with restart-aware
  suppression and recovery evidence. Inspect the existing LLM-budget producer and
  add its mandatory alert only where authoritative budget state exists. Then run
  broader complete-suite verification and audit remaining PAPER notification/EOD
  requirements before progressing to the remaining supervised/AI layers.

## Provider-health honesty and persistent health alerts (2026-09-27)

- Fixed a genuine false-health defect: MarketDataCheck treated provider FAIL or
  absent status as PASS. It now preserves explicit status and rejects invalid
  evidence. LiveMarketDataProvider no longer appears healthy before connection,
  before observations, after close, or with stale/future tracked observations.
  Fresh REST fallback remains DEGRADED; invalid feed prices cannot renew freshness.
- Watchdog state now uses a verified per-mode audit chain. Unchanged conditions
  deduplicate across restart; auth/feed FAIL transitions emit typed CRITICAL
  outbox requests and subsequent PASS emits INFO, including intermediate degraded
  states. Audit corruption prevents re-arm. Other risk/emergency gates remain intact.
- Full collected-suite coverage in completed disjoint batches: **1090 passed / 0
  failed / 3 PostgreSQL-dependent skipped**. Unit: 637; integration: 191 (+3 skips),
  94, 60, 45, 58, and 5 real browser scenarios. Logs:
  `backend/logs/provider-health-unit.txt`, `provider-health-integration-a.txt`,
  `provider-health-integration-b1.txt`, `provider-health-integration-b3.txt`,
  `provider-health-integration-c1.txt`, `provider-health-integration-c2.txt`,
  `provider-health-browser.txt`. Source was frozen across this coverage. Frontend:
  **10 passed**, production build successful. Existing dependency warnings remain.
- No new requirement promoted: NOTIF-003 and MON-005 remain partial; MD-001/010
  remain externally unverified. Counts: **223 tested / 13 unverified / 88 partial /
  209 not started**, total 533. LLM inspection found configuration and the call
  database model but no authoritative budget/provider service (LLM-001 onward are
  still not started). No fictitious LLM-budget event was added.
- Important integration gap discovered: startup health constructs a different,
  unconnected market-data provider from the worker. The previous false PASS masked
  that gap. Strict checks correctly keep it unavailable rather than inventing
  connectivity. The supplied calendar/data/credentials and real deployment remain
  unverified; no M1/M2 or LIVE completion claim is made.
- Exact next task: bind market-data health to the worker's actual selected provider
  and add safe read-only quote/input refresh before entry gates, so health recovery
  does not depend on permission to trade. Exercise real worker/watchdog startup
  with external-provider fixtures, unavailable/stale data and independent risk
  latches. Preserve fresh-input checks and truthful DEGRADED REST status; do not
  silence critical checks or introduce fake startup success. Handle expected
  reference warm-up unavailability as audited stand-down rather than inventing
  candles. Then finish PAPER EOD summary/notification integration and the remaining
  AI/provider layer, with LLM budget notifications only when its producer exists.

## Selected-provider bootstrap integration (2026-09-27)

- Removed the disconnected PAPER startup data-provider instance. Health now reads
  the actual selected worker provider and its committed quote observations.
  Read-only quote refresh runs before entry permissions, avoiding a startup
  deadlock where unavailable data prevented the collection needed for recovery.
- Critical input health requires fresh, validated, provenance-bearing quotes and
  verified observation audit chains. Transport is reported separately: REST
  fallback remains visibly DEGRADED, not falsely websocket-connected. Transport
  availability alone cannot grant permission to trade. Existing strategy, sizing,
  risk, preflight, protection and owner-review gates remain authoritative.
- Insufficient reference candle warmup creates an audited stand-down, not an
  invented signal or a worker-error latch. Invalid/stale quote refresh clears
  cached observations and fails closed. Bootstrap integration tests cover actual
  watchdog recovery, a resulting PAPER order/API state, independent risk latch,
  stale replacement observations and warmup stand-down.
- Verified this checkpoint: **692 passed / 0 failed / 0 skipped** across all 637
  unit tests and 55 selected worker/ingestion/watchdog/recovery integration tests.
  Logs: `backend/logs/market-bootstrap-unit.txt`, `market-bootstrap.txt`.
  This is targeted coverage, not a new full-suite result. Last full coverage at
  `aab5416`: 1090 passed / 0 failed / 3 PostgreSQL skips. Frontend unchanged; last
  verified frontend result remains 10 passed and a successful production build.
  Changed trading modules/new test pass Ruff and formatting checks; pre-existing
  startup/main lint findings and two dependency deprecation warnings remain.
- Requirements counts unchanged: **223 tested / 13 implemented-unverified / 88
  partial / 209 not started**. MON-005 remains partial; external data, Groww LIVE,
  actual remote notification delivery and M1/M2 remain unverified.
- Exact next task: implement a durable, idempotent PAPER session-end summary from
  actual persisted trading/account evidence, linked to audit and notification
  outbox. Preserve gross/charges/net distinctions and explicit unavailable values;
  cover restart deduplication, session timezone/calendar boundaries, open positions
  and failed reconciliation without claiming a successful close. Then complete
  remaining PAPER gaps and the advisory AI/provider layer.

## Durable PAPER session-end observations (2026-09-27)

- The worker now records a first post-close session-day summary and notification
  request in one database transaction. A verified deterministic audit chain
  deduplicates subsequent cycles and restarts. Calendar completeness, actual
  session close and IST day boundaries gate publication; SQL cutoffs use UTC.
- Content derives from persisted FIFO fill deltas, estimated charges, actual open
  positions, unresolved orders and critical source-audit events. Missing tariffs
  yield unavailable charges/net P&L, never zero invented fees. Failed close cycles
  explicitly report reconciliation NOT_CONFIRMED, remaining positions and review
  requirements, not a successful session close. Existing authenticated workspace
  notification/audit APIs expose the result; no new static frontend pages.
- NOTIF-005 is now **partial**, not completed. The summary explicitly covers only
  the first post-close observation, not proven full-session coverage. Daily-loss
  and drawdown utilisation require session MTM evidence and remain unavailable;
  missed-session catch-up and recovery follow-up summaries remain pending.
  Open-position count and configured concurrency limit/version are included.
- Exact next task: assemble auditable session-end valuation/limit-utilisation
  evidence and add recovery follow-up without rewriting the original
  summary. Then address missed-session catch-up and remaining PAPER/AI gaps.
  Counts: **223 tested / 13 implemented-unverified / 89 partial / 208 not started**.
  PAPER remains default; remote notification delivery, real market data, Groww
  LIVE and M1/M2 remain unverified.
- Failure-path verification also covers rollback when outbox enqueue fails after
  the summary append: neither record commits. Worker failure is re-persisted to
  heartbeat after publication failures, so restart cannot silently erase the
  owner-review requirement. A successful later publication does not re-arm it.
- Verification: **697 passed / 0 failed / 0 skipped** (637 unit and 60 selected
  worker/ingestion/watchdog/recovery/summary integration tests), recorded in
  `backend/logs/paper-eod-summary.txt`. This is not a new full-suite claim; the
  latest full collected-suite coverage remains 1090 passed / 3 PostgreSQL skips
  at `aab5416`. Frontend **10 passed** and production build successful; the first
  frontend attempt hit sandbox esbuild spawn EPERM, then passed with permitted
  process execution. Changed summary/worker/test files pass Ruff. No external
  notification delivery was exercised.

## Session-end valuation and recovery evidence (2026-09-27)

- Session summaries now include account equity/cash, unrealised P&L, gross
  exposure, observed equity peak, source fill/snapshot IDs and marked-position
  evidence. Daily-loss, drawdown and exposure monetary limits use shared pure
  helpers also consumed by the existing risk rules/latches; no limits or veto
  conditions were relaxed. Values remain PAPER estimates, not broker billing.
- Valuation requires a recent successful reconciliation, known historical fill
  charges and fresh broker/local-consistent position marks. Unknown costs,
  unconfirmed reconciliation, stale marks and future account fills yield an
  explicit unavailable reason. Future portfolio snapshots cannot set the peak.
  Tests confirm that exit charges can produce a small drawdown from the pre-exit
  observed peak even when the completed trade has positive net P&L.
- Recovery appends a linked summary/notification rather than rewriting the first
  observation. Repeated unchanged recovery cycles deduplicate. Closing a formerly
  unprotected-by-fresh-data position does not clear persistent owner review.
- NOTIF-005 remains partial and counts remain **223 tested / 13 unverified / 89
  partial / 208 not started**. The existing workspace shows notice identities and
  source audit IDs, not the full summary body: a typed summary read API and its
  existing-page UI integration are still required. Remote delivery, missed-session
  catch-up, complete P&L periods, real feed/Groww LIVE and M1/M2 remain unverified.
- Exact next task: fix the execution portfolio-state query's missing upper
  timestamp bounds (its peak/day-fill queries can currently include future stored
  evidence), with adversarial pipeline tests. Then expose verified summary bodies
  through a typed read API and the existing monitoring page, add monetary limits
  to the delivery text, and implement truthful missed-session recovery.
- Full collected-suite coverage: **1101 passed / 0 unresolved failures / 3
  PostgreSQL-dependent skips**. The first run passed 353 tests, then caught a
  provider-session fixture reusing the previous day's quote after the read-only
  bootstrap change. The next-day fixture now supplies a fresh timestamp; stale
  rejection/latching is unchanged and separately tested. The repaired test and
  all remaining tests passed in a 743-test batch. Five opt-in real browser tests
  also passed, replacing their skips in the initial run. Production source was
  frozen throughout; only the identified fixture changed between these batches.
  Logs: `backend/logs/eod-valuation-full.txt`, `eod-valuation-remainder.txt`,
  `eod-valuation-browser.txt`. Frontend **10 passed**, production build successful.
  Changed files pass Ruff and formatting checks. No external dependency claim.

## Execution time guards and connected session-report UI (2026-09-27)

- Execution portfolio queries now capture a single UTC cutoff, exclude future
  peak snapshots and bound daily fill/charge queries. Future actual fills or
  position lifecycle/mark timestamps fail closed because the current mutable
  broker account cannot honestly reconstruct an earlier account state. Tests run
  genuine pipeline-approved second candidates through execution and prove no
  additional broker order is sent for each contaminated timestamp.
- Added authenticated `GET /api/v1/reports/paper/daily` with a typed whitelisted
  response, bounded result count, whole-chain audit verification and observation
  timestamp checks. Corrupt or future report evidence returns an explicit 409;
  it is not shown as a valid financial report. Regenerated checked-in OpenAPI and
  TypeScript contracts from the actual backend.
- The existing Monitoring page now shows the report body: gross/net/charges,
  unavailable cost evidence, positions/orders/errors, monetary limit utilisation,
  reconciliation and owner-review state, observation time and audit/recovery IDs.
  Refresh is read-only, every ten seconds or manually. Loading, empty and failed
  states are distinct, and failed reads remove stale report figures. A real
  browser test follows the costed reference trade to its session summary and
  observes the automatic API poll, without mocking application layers.
- Delivery text now includes the shared daily-loss/drawdown/exposure monetary
  utilisation or explicit UNAVAILABLE. No remote channel delivery is claimed.
  Counts remain **223 tested / 13 unverified / 89 partial / 208 not started**;
  NOTIF-005 remains partial. PAPER default, Groww LIVE unverified, no M1/M2 claim.
- Exact next task: recover summaries missed while the worker was offline without
  using today's mutable positions/account as yesterday's evidence. Build from
  immutable, cutoff-bounded fills and verified observations; explicitly mark
  unavailable historical marks, coverage, risk configuration or reconciliation.
  Exercise restart/catch-up idempotency and expose the observation scope in the
  same typed API/UI. Then progress to the remaining PAPER and advisory-AI gaps.
- Verification: **726 passed / 0 failed / 0 skipped** in this checkpoint's
  selected coverage: 637 unit tests, 83 targeted integration tests and 6 actual
  browser scenarios. Logs: `backend/logs/summary-dashboard-regression.txt` and
  `summary-dashboard-browser.txt`. Frontend **12 passed**, production build
  successful. This is not a new full-suite claim; the last complete collected
  coverage at `9988e62` remains 1101 passed with 3 PostgreSQL-dependent skips.
  Changed feature files pass Ruff; the existing dependency warnings remain.

## Missed-session PAPER reconstruction (2026-09-27)

- Found and addressed a prerequisite gap: open fills had mutable Trade rows and
  order-status audit, but no standalone immutable economic fill snapshot. New
  PAPER fills now seal quantity/price/side/time/cost/sequence and order/position
  linkage in the same transaction as local accounting. Seal failure rolls back
  local fills/positions; restart retrieves the already-filled broker order and
  records it once, retaining owner review rather than submitting a duplicate.
- The worker catches up at most three missed sessions per cycle, using the
  configured complete trading calendar and existing activity evidence. It uses
  only fill seals both recorded and executed by the historical close, recomputes
  FIFO per position and never reads today's mutable Position as yesterday's
  holding. Root summary records and source audit chains are verified; each
  recovered day and outbox request commit together and deduplicate on restart.
- Integration tests prove yesterday's recorded open quantity remains 111 even
  after today's actual exit leaves the current position flat. The later exit/P&L
  cannot leak backward. Missing legacy/cutoff fill evidence yields null economics
  and unavailable holdings, not zero invented trades. Corrupt fill evidence
  fails closed. Past critical audit events are verified and included.
- The typed report API/UI distinguishes MISSED_SESSION_RECOVERY, historical
  unknown owner review, unavailable order history and unavailable economics.
  Historical marks, limits, reconciliation and full-session coverage remain
  explicitly unverified; immutable order-transition/configuration evidence is
  still needed for fuller historical reconstruction. No legacy seals were
  fabricated, and no past report claims real broker/external validation.
- Requirements counts unchanged: **223 tested / 13 unverified / 89 partial / 208
  not started**. NOTIF-005 and AUDIT-001 remain partial. PAPER remains default;
  Groww LIVE, remote delivery and M1/M2 remain unverified.
- Exact next high-value PAPER task: add authenticated trade/decision audit
  drill-down to the existing Audit page (AUDIT-003), exposing verified proposal,
  evidence, sizing, risk, order/fill/journal and rejection linkage so users can
  answer why a trade did or did not occur from the UI. Preserve source snapshots
  and show missing/corrupt links explicitly. Historical order/configuration
  reconstruction and broader PAPER/AI layers remain on the pending ledger.
- Verified checkpoint coverage: **730 passed / 0 failed / 0 skipped** (637 unit,
  87 targeted integration and 6 real browser tests). Logs:
  `backend/logs/missed-summary-regression.txt`, `missed-summary-browser.txt`.
  Frontend **13 passed**, production build successful; changed files pass Ruff
  and formatting checks. This is selected coverage, not a new complete-suite
  claim. Last complete collected coverage at `9988e62`: 1101 passed with 3
  PostgreSQL-dependent skips. Existing dependency deprecation warnings remain.

## Authenticated PAPER audit drill-down (2026-09-27)

- Added authenticated `/api/v1/audit/events` and `/api/v1/audit/trails/{identifier}`
  with typed OpenAPI contracts. Search supports instrument, IST session day,
  decision/proposal/candidate/trade IDs, bounded pagination and mode filtering.
  The existing Audit view now searches events and inspects linked chains rather
  than only dumping the workspace's recent audit rows.
- Reads verify stored hash chains before displaying immutable decision inputs,
  sizing/risk explanations, order/fill evidence and sealed journal economics.
  Corrupt or missing chains produce explicit gaps; corrupt payloads are withheld.
  Journal economics are displayed only when their seal matches. Future audit
  records are excluded from discovery and refused in chain inspection. Mutable
  table associations are discovery aids, not claimed completeness anchors.
- Actual costed reference-worker integration proves the 856.56 fixture net P&L
  and original sizing/risk evidence can be inspected by fill ID. Negative
  confidence rejection is searchable by candidate/instrument/day, with missing
  evidence explicit. Authentication, query bounds and corrupt payload withholding
  are tested. Real browser coverage opens the same trade in the existing Audit
  page, expands its decision and journal, and still verifies all PAPER exit views.
- AUDIT-003 advances only to partial: legacy linkage, trusted completeness
  anchors and full external/LLM/mode parity remain unverified. Counts: **223
  tested / 13 implemented-unverified / 90 partial / 207 not started**. No
  additional requirement is marked complete. PAPER remains default; Groww LIVE,
  actual notification delivery, PostgreSQL deployment and M1/M2 remain unverified.
- Initial browser regression caught a stale table-cell locator after replacing
  the old Audit table. Updated it to actually inspect the returned audit ID;
  rerun passes. Frontend 14 tests pass and production build succeeds. Selected
  backend coverage: 669 regression tests plus six real browser scenarios pass.
  Full suite including browser scenarios: **1113 passed / 0 failed / 3 skipped**
  in `backend/logs/audit-full.txt`. Skips require PostgreSQL; two existing
  dependency deprecation warnings remain. Changed feature files pass Ruff.
- Exact next high-value PAPER task: audit refused emergency/control requests
  (EMG-006). Current successful mutations are audited, but typed-confirmation,
  mode, unavailable-worker and failed-clear refusals need authenticated actor,
  reason and non-success outcome evidence without weakening safety gates or
  persisting secrets. Verify refusal visibility through this Audit view, then
  continue the pending PAPER safety/integration ledger.

## Emergency refusal evidence (2026-09-27)

- Authenticated typed emergency requests now record refused confirmation, mode,
  missing recovered worker and failed health-clear outcomes. Unexpected failures
  record only the exception class, not sensitive external exception text. The
  original safety exception/status still propagates; no refusal becomes success.
- Refusals use independent hash chains, never the authoritative `paper-emergency`
  state chain. State mutation is not inferred from a failed request: the record
  directs inspection of the control chain. Flatten without a worker explicitly
  records that entries were blocked but no flatten execution occurred. No typed
  confirmation text is persisted. Audit reads expose the records to the existing
  Audit view. Direct clear also rejects a missing actor or blank reason.
- Tests cover owner attribution, chain integrity, API drill-down, maintained kill
  state after refused clear, no broker orders, storage failure, and omission of
  raw exception/confirmation text. An unavailable audit store cannot fabricate a
  durable refusal record; the request fails and must not be reported successful.
  Schema-invalid and unauthenticated requests remain outside this typed owner
  action hook. Non-PAPER execution remains unavailable; EMG-006 stays partial.
- Counts unchanged: **223 tested / 13 implemented-unverified / 90 partial / 207
  not started**. PAPER default, Groww LIVE unverified. Frontend code/contracts
  unchanged from the 14-test successful build at `c50b18b`.
- Next highest-value PAPER unit: inspect and integrate stale pending entry-order
  cancellation and EOD order hygiene (OMS-006/007) with the running worker.
  Preserve protective exits, record per-order outcomes, reconcile ambiguous
  cancellations and survive restart without duplicate submission. Existing
  cutoff cancellation must be reused rather than replaced.
- Verified selected checkpoint coverage: **693 passed / 0 failed / 0 skipped**,
  including all unit tests and six real browser scenarios. Log:
  `backend/logs/emergency-regression.txt`. Ruff passes for changed feature files.
  Last complete suite at `c50b18b`: 1113 passed, three PostgreSQL-dependent skips;
  this checkpoint does not claim a new full-suite run or external verification.

## PAPER pending-order hygiene (2026-09-27)

- Added a worker-integrated entry-order sweeper with configurable
  `PAPER_ENTRY_MAX_AGE_SECONDS=300`. Age uses persisted submission/creation time;
  future timestamps fail closed. Historical runs capture the setting so replay
  cannot silently substitute a different cancellation policy.
- Cutoff reuses this service for all non-terminal ENTRY states, not just
  OPEN/PENDING/PARTIAL. It attempts every selected cancellation and still attempts
  position exits after a cancellation failure. Protective EXIT orders are not
  cancelled by age. Fills/positions remain intact, and unresolved outcomes block
  new entries while worker review remains latched.
- Cancellation intent is audited before the broker call; observed status/fills
  are recorded afterwards and linked to order/proposal/instrument. An interrupted
  result is recovered on restart even if the broker/local order is already
  terminal. No duplicate submission occurs. Fixed cancellation's post-lookup
  race: a newly discovered terminal order is not sent another cancel request.
- Deterministic tests use the actual risk/preflight/PAPER OMS with a depth change
  after preflight, rather than bypassing liquidity checks. They cover the age
  boundary, partial fills, restart, broker failure/retry, future timestamps,
  retained pending exits, failure before durable intent and interruption after
  broker cancellation but before audit result. See `docs/PAPER_ORDER_HYGIENE.md`.
- OMS-006/007 advance to partial only. Separate cancellation journal integration,
  generalized limit/live order handling and a comprehensive EOD outcome report
  remain pending. Counts: **223 tested / 13 implemented-unverified / 92 partial /
  205 not started**. PAPER remains default; Groww LIVE and M1/M2 remain unverified.
- Exact next task: link cancellation decisions into the append-only journal
  without pretending unfilled/partially filled cancellation is a closed trade,
  then expose a verified EOD order-hygiene report with unresolved order IDs and
  reconciliation/protection outcomes through the existing API/UI.
- Verified checkpoint: **701 passed / 0 failed / 0 skipped** in
  `backend/logs/hygiene-regression.txt`, including all unit tests, historical
  replay/worker integration and six real browser scenarios. Feature files pass
  Ruff. Frontend sources/build are unchanged from the 14-test verified build;
  the browser suite exercised that bundle against the new backend. Last complete
  suite remains `c50b18b`: 1113 passed, three PostgreSQL-dependent skips.

## Cancellation journal and EOD evidence (2026-09-27)

- Cancellation result audit and sealed `ORDER_ACTION` journal now commit in one
  transaction. A failed journal seal rolls back the result too; restart completes
  the surviving intent without duplicate submission or journal. Actual partial
  fills remain open positions until a real simulated exit, and action records
  never invent P&L, fees, exit price or a closed-trade timestamp.
- Existing Journal API/filter/UI supports ORDER_ACTION. These records retain
  proposal/risk/order/audit linkage and seal verification. Trade/rejection-only
  correction controls are not offered for actions; annotations remain separate.
  Existing performance/risk consumers continue selecting TRADE records.
- Existing Monitoring summaries expose verified session cancellation actions,
  unresolved order IDs and recorded protection failures. Source chains are
  verified and event timestamps bounded by observation time. Corrupt evidence
  refuses report generation. Legacy/missed-session action history remains
  explicitly unavailable; absence of failures is not claimed as protection.
- Actual partial-fill → cutoff cancellation → exit → journal → EOD API/UI browser
  scenario passes. Seal rollback/recovery, quantity retention, no fictional
  economics, historical cutoff exclusion and corrupt source refusal are tested.
  Frontend 14 tests/build pass; seven real-browser scenarios pass. Complete
  backend/browser suite: **1123 passed / 0 failed / 3 PostgreSQL-dependent skips**
  in `backend/logs/order-journal-full.txt`. Two existing dependency deprecation
  warnings remain. Changed feature files pass Ruff; no external verification is
  inferred from these tests.
- OMS-006/007 remain partial for generalized limit/live handling and complete
  cross-mode/session evidence. Counts unchanged: **223 tested / 13
  implemented-unverified / 92 partial / 205 not started**. PAPER default; Groww
  LIVE, external delivery, PostgreSQL deployment and M1/M2 remain unverified.
- Exact next high-value PAPER task: authenticated standalone kill CLI (EMG-005)
  that works with the web application stopped, writes the same durable audited
  control state, and is honoured by the worker without a bypass or automatic
  re-arm. Test against a shared persisted database and document Windows usage.

## Standalone authenticated PAPER kill (2026-09-27)

- Added `python -m app.emergency.cli kill` using existing OwnerAuth and
  EmergencyControls, not a parallel emergency state or HTTP dependency. Password
  is prompted invisibly; bounded stdin input is explicitly opt-in. Passwords,
  tokens and raw configuration/database errors are never printed by the command.
- The owner supplies a substantive reason and `KILL PAPER` confirmation. Normal
  login lockout applies; the temporary authenticated session is revoked. Refusals
  remain audited. Success means durable entry inhibition only, never flatten or
  a claim of closed positions. No reset/re-arm or non-PAPER authority is exposed.
- Actual child-process tests use the worker's shared isolated database without
  an HTTP server. A running worker honours the kill on its next cycle; restart
  preserves it; execution rejects the pre-existing approved proposal. Bad
  credentials/confirmation and unavailable schema cannot report success. Windows
  instructions and failure/operational limitations are in `docs/EMERGENCY_CLI.md`.
- EMG-005 advances to partial: PAPER application integration is verified, but
  other modes and live PostgreSQL deployment remain unverified. Counts: **223
  tested / 13 implemented-unverified / 93 partial / 204 not started**. PAPER
  remains default; Groww LIVE and M1/M2 remain unverified.
- Exact next task: independently verify automatic emergency-trigger integration
  (EMG-004) across loss/drawdown, reconciliation, repeated order rejection and
  critical health/feed outage. Reuse existing risk latches/incidents; implement
  missing paths and prove durable entry blocking, continued safe exits, alerts
  and restart behaviour through actual PAPER services rather than new adapters.
- Verified checkpoint coverage: **689 passed / 0 failed / 0 skipped**, including
  all unit tests and seven real browser scenarios; log:
  `backend/logs/emergency-cli-final.txt`. A terminal unable to suppress password
  echo is explicitly refused and tested. The first regression process exited
  interrupted and was verified terminated before rerunning; it is not counted.
  Frontend sources/build remain the verified 14-test bundle. Last complete suite
  at `d9eb30c`: 1123 passed, three PostgreSQL-dependent skips. Changed files pass
  Ruff; this is selected coverage, not a new full-suite claim.

## Automatic PAPER rejection control and trigger verification (2026-09-27)

- Independent inspection found existing durable loss/drawdown latches, critical
  health/feed gating and reconciliation incident notices, but repeated broker
  rejections only notified. Added a distinct-order broker-rejection trigger using
  the existing emergency entry control, not a second risk engine or LLM authority.
- `PAPER_BROKER_REJECTION_LIMIT=3` is configurable (1–100) and captured in
  historical execution settings. First observations count per IST day; duplicate
  polls, including next-day polls, cannot inflate the count. Local preflight
  refusals do not count. Evidence records source/order IDs, count and threshold.
- EmergencyControls supports the caller's transaction so order synchronization,
  rejection evidence, durable entry inhibition and critical notification requests
  are atomic. Tests force outbox failure, verify rollback, then recover the same
  rejected broker order exactly once. The block survives restart and requires
  existing explicit owner clear; daily count history is not erased by clear.
- Actual PAPER rejection fixtures prove a pre-approved subsequent order cannot
  dispatch after the threshold, and a rejected exit can still close through the
  existing safe replacement path. Isolated outbox delivery proves one threshold
  notice rather than repeated notices from duplicate polls. No real external
  delivery or Groww execution claim is made. See `docs/AUTOMATIC_PAPER_CONTROLS.md`.
- EMG-004 advances to partial, not complete: deployed/cross-mode trigger and
  notification validation remain unverified. Counts: **223 tested / 13
  implemented-unverified / 94 partial / 203 not started**. PAPER remains default;
  no M1/M2 completion claim.
- Next highest-value integration: verify and complete backtest universe/simulation
  disclosures (BT-010/011) across actual persisted reports, API, export and existing
  dashboard. Preserve the real construction method and distinguish historical
  eligibility evidence from owner-selected samples; never claim survivorship
  protection or live performance from synthetic/replayed results.
- Verified selected coverage: **728 passed / 0 failed / 0 skipped** across three
  completed batches: 638 unit, 83 integration and seven real browser tests. Logs:
  `backend/logs/trigger-unit.txt`, `trigger-integration.txt`, `trigger-browser.txt`.
  An earlier combined process terminated interrupted and is not counted; its
  terminal handle was confirmed before rerunning. Changed feature files pass
  Ruff. Frontend remains the 14-test verified production bundle. Last complete
  suite at `d9eb30c`: 1123 passed with three PostgreSQL-dependent skips; no new
  full-suite or external-delivery verification claim is made.

## Historical universe disclosure and read integrity (2026-09-27)

- New historical and walk-forward reports persist the actual owner-declared
  static input lists, including all frozen child windows, source/knowledge times,
  active/restricted flags and strategy-versus-context roles. Removed wording that
  implied externally verified point-in-time eligibility. Listing/delisting and
  completeness remain unverified; survivorship/selection bias is explicitly possible.
- The existing Backtests page renders these API-backed disclosures. Missing legacy
  metadata is unavailable, not fabricated or backfilled into sealed reports.
  Existing publication preserves the disclosure in its sealed catalog digest.
- Inspection found ordinary historical reads were not checking their final audit
  binding (walk-forward reads already did). Detail/trade/sample reads now reject
  tampered finalized catalogs, including false simulation flags. All three payloads
  explicitly carry simulated=true. Internal integrity is not external certification.
- Verified **665 passed / 0 failed / 0 skipped**: 638 unit, 25 selected integration,
  two actual historical/walk-forward browser scenarios (five browser scenarios
  deselected). Frontend **14 passed**, production build successful. Logs:
  `backend/logs/disclosure-unit.txt`, `disclosure-integration.txt`,
  `disclosure-browser.txt`. An initial invalid test path collected nothing and was
  corrected; it is not counted. No new full-suite claim. Last complete suite at
  d9eb30c remains 1123 passed with three PostgreSQL-dependent skips.
- BT-010/011 advance to partial: report/API/UI integration is verified; standalone
  export and the complete payload-labeling sweep remain pending. Counts: **223
  tested / 13 implemented-unverified / 96 partial / 201 not started**.
- Exact next task: add bounded authenticated historical report JSON export through
  the existing Backtests page, preserving simulation/universe/integrity disclosure,
  and verify download/tamper/authorization behavior. Then finish the simulation
  labeling sweep across historical job/CLI payloads before reassessing BT-010/011.
  PAPER remains default; Groww LIVE, external delivery, M1 and M2 remain unverified.

## Authenticated bounded historical export (2026-09-27)

- Added typed `GET /api/v1/backtests/{run_id}/export` and a download action in the
  existing Backtests page. Actual sealed report metrics/curves, closed trades,
  OOS source links and persisted universe disclosures are exported together with
  simulated=true and the catalog digest. Credentials/raw execution settings and
  arbitrary manifest parameters are not exported. This is not an audit backup.
- Export validates the exact loaded catalog against its final audit binding,
  requires AUDIT_BOUND simulated evidence and refuses unfinished/unbound/tampered
  reports. More than 10,000 records per collection or 16 MiB is refused, never
  truncated. The owner, counts and exported byte hash are audited separately;
  audit failure cannot report success or mutate the source report's seal.
- Actual historical and two-window OOS integration tests verify contents, source
  linkage, auth refusal, size bounds, hash audit and tamper refusal. A real browser
  downloads and parses the JSON from the running API, checking simulation/universe
  and costed trade state. Frontend remains **14 tests passed**, production build
  successful. No new page or static data was introduced.
- Full backend suite including all seven real browser scenarios: **1130 passed /
  0 failed / 3 PostgreSQL-dependent skips**, two dependency deprecation warnings.
  Log: `backend/logs/report-export-full.txt`. Focused export run: seven passed,
  `backend/logs/report-export.txt`. Changed Python files pass Ruff.
- BT-010/011 remain partial pending legacy-disclosure acceptance and the remaining
  historical job/CLI payload labeling sweep. Counts unchanged: **223 tested / 13
  implemented-unverified / 96 partial / 201 not started**. PAPER remains default;
  Groww LIVE and M1/M2 remain unverified.
- Exact next task: verify legacy disclosure behavior and finish simulation labels
  across discovery, result/job/plan and CLI payloads without converting false or
  missing underlying evidence into certified success. Re-run relevant real jobs
  and dashboard flows, then reassess BT-010/011 against their actual acceptance.

## Legacy report limitations and simulation labeling (2026-09-27)

- Finished the historical payload sweep: discovery/results/trades/samples,
  job/plan/launch records, OOS review and runner/publication CLI carry simulated=true.
  Controllers reject missing/false child labels; discovery refuses a stored false
  marker rather than silently relabeling it. Failure remains failure, and a plan
  or simulation label never proves a completed run or LIVE performance.
- Legacy finalized reports with no structured universe disclosure retain null
  metadata and unchanged sealed history. Detail/export add an explicit warning
  about unavailable construction evidence and possible survivorship/selection bias;
  the existing UI shows unavailable rather than inventing historical eligibility.
  A real worker fixture seals a legacy-shaped report and proves reads/exports do
  not backfill or alter its source metadata. New-run/WF disclosure and real-browser
  download tests continue to verify actual lists, roles and audit-bound results.
- Verified **678 passed / 0 failed / 0 skipped**: 676 selected unit/integration and
  two real historical/WF browser scenarios (five browser scenarios deselected).
  Frontend **14 passed**, production build successful; changed Python files pass
  Ruff. Logs: `backend/logs/simulation-labels.txt`, `simulation-browser.txt`.
  Last complete suite at d7fad76: **1130 passed / 0 failed / 3 PostgreSQL skips**.
- BT-010/011 now satisfy their integrated disclosure/labeling acceptance. This
  does not claim actual survivorship protection or external historical accuracy.
  Counts: **225 tested / 13 implemented-unverified / 94 partial / 201 not started**.
- Exact next task: independently verify BT-003 against the actual historical
  runner and existing PaperFillEngine. Exercise configured spread/depth, adverse
  slippage, partial fills and rejections through persisted report outcomes; expose
  any real fill-model limitations without building a duplicate simulation engine.
  PAPER remains default; Groww LIVE, deployed PostgreSQL and M1/M2 remain unverified.


## Historical fill verification and realism limitations (2026-09-28)

- Extended actual isolated historical runs, not a parallel simulator: 0/10/20 bps
  fallback slippage yields independently calculated exit prices 108/107.89/107.78
  and gross P&L 888/875.79/863.58 for 111 units entered at 100. Persisted net P&L
  subtracts actual configured charges and declines monotonically. Depth-enabled
  execution uses the supplied book rather than this fallback penalty.
- Actual rejection probability=1 produces rejected OMS orders and no fills/closed
  trades; partial probability=1 produces audited partial fills and an INCOMPLETE
  report rather than fabricated full success. Fixtures remain isolated test data.
- Fixed a genuine reporting defect: depth-disabled manifests previously claimed
  RECORDED_DEPTH_AT_PUBLICATION. They now record RECORDED_LTP_WITH_ADVERSE_SLIPPAGE.
  Existing depth behavior is preserved. No new execution abstraction was added.
- Inspection also found latency_ms was metadata only, not an execution delay;
  repeated evaluations do not conserve the same displayed depth globally. Removed
  an inaccurate engine docstring claiming exact market replication and documented
  scope/limitations in `docs/HISTORICAL_FILL_MODEL.md`. No live realism claim.
- PAPER-004 is honestly downgraded to partial; BT-003/EXEC-014 advance to partial,
  not completion. Counts: **224 tested / 13 implemented-unverified / 97 partial /
  199 not started**. These changes correct evidence, not optimize test counts.
- Verified **690 passed / 0 failed / 0 skipped** across all unit tests and relevant
  PAPER broker/historical runner/API/publication regressions. Log:
  `backend/logs/fill-regression.txt`; focused real-process cases also passed (four,
  `historical-fills.txt`). Frontend unchanged from verified 14-test production
  bundle. Last complete suite at d7fad76 remains 1130 passed, three PostgreSQL skips.
- Exact next task: implement persisted, clock-driven PAPER latency and consistent
  snapshot-depth consumption in the existing provider, with restart/cancel/partial
  order tests and actual historical-worker parity. Do not use wall-clock sleeps in
  replay or introduce future quotes. Preserve existing zero-latency fixtures
  explicitly; verify default PAPER behavior rather than silently bypassing delay.
  PAPER remains default; Groww LIVE, deployed PostgreSQL and M1/M2 remain unverified.

## Persisted PAPER latency and execution-time margin (2026-09-28)

- Added persisted order eligibility deadlines, clock-driven settlement
  and historical deadline advancement; no wall-clock sleeps or future-quote fills.
  Submission/modify delay survives restart; cancellation prevents a delayed fill.
  Existing immediate-fill fixtures now explicitly request zero latency.
- A genuine delayed-entry edge case was reproduced: a fresh quote can precede
  the entry fill. The reference exit monitor now audits a bounded wait for its
  first post-entry mark while independent fixed protection remains required.
  Existing exit-state timestamp regression and stale-input rejection remain intact.
- A fill-time margin preview uses existing account/fee arithmetic to reject
  unaffordable exposure increases after prices/funds change. Tests exercise both
  competing pending orders and changed prices without blocking reducing exits.
- The recorded-worker acceptance test was updated to visit actual default 150 ms
  entry/exit deadlines, verifying pending state first instead of assuming immediate
  execution. Focused regression: 47 passed in `logs/latency-final-focused.txt`.
- Complete test selection verified in independently completed batches: **1140
  passed / 0 failed / 3 PostgreSQL-dependent skips**, including all seven real
  browser scenarios. Frontend **14 passed**, production build successful. This is
  full selection coverage in separate processes, not a single-process suite claim.
  Logs: `backend/logs/latency-final-batch-0.txt` through `-8.txt` (each ends with
  VALIDATION_EXIT_CODE=0), plus `latency-final-groww.txt` (76 isolated adapter tests,
  not live Groww). Counts per batch: 638, 73, 46, 53, 86, 57, 45, 59, 7; nested
  adapter tests add 76. Two existing dependency deprecation warnings remain.
- Earlier full-suite attempts were interrupted by runner/host turnover and are
  not counted. Their recorded-worker failure was reproduced and fixed by visiting
  real execution deadlines, not hiding it with zero latency. Logs from the final
  batches mix PowerShell encodings; removing NUL characters makes their captured
  summaries readable without changing the preserved raw evidence.
- Changed modern modules/tests pass Ruff; the older PAPER engine/provider retain
  the same 34 pre-existing style findings as HEAD (verified against git versions),
  not falsely reported lint-clean. No unrelated bulk style cleanup was performed.
- EXEC-014, BT-003 and PAPER-004 remain partial: snapshot-depth consumption is not
  yet conserved across repeated evaluations/orders. Counts remain **224 tested /
  13 implemented-unverified / 97 partial / 199 not started**.
- Exact next task: conserve snapshot-depth liquidity in the existing PAPER provider
  across orders, partial fills, polling and restart. Persist enough fill/snapshot
  evidence to prevent reusing the same displayed quantity; verify newer snapshots
  explicitly replenish capacity without leaking future quotes. Reuse existing
  engine/account/OMS boundaries and test actual historical-worker parity. PAPER
  remains default; Groww LIVE, deployed PostgreSQL and M1/M2 remain unverified.

## In-progress snapshot-depth conservation (2026-09-28)

- Working tree adds original snapshot evidence to persisted PAPER fills and
  reconstructs consumed instrument/side capacity from that evidence. Products
  share the budget; polling/cancellation/restart cannot replenish it. Newer
  observations replenish capacity explicitly; same-time consumed-side changes,
  regressed observations, corrupt fingerprints and ambiguous legacy fills fail
  closed. No parallel execution engine or schema migration was introduced.
- Durable broker fixtures cover shared MIS/CNC quantities, independent sides,
  cancellations, restart, seeded rejection, conflicting/older snapshots and legacy
  data. Actual historical worker fixtures prove repeated one-second polling fills
  only 40 of an approved 111-unit limit order; a single later snapshot permits 80,
  then actual OMS exits/journals record gross P&L of 320/640, not fictitious full fills.
- Initial broad tests exposed an over-strict whole-book identity check: changes
  to an unused opposite side must not block its independent budget. Corrected to
  compare the consumed side while retaining original snapshot integrity evidence.
  All 27 targeted regressions now pass (`logs/liquidity-sides.txt`). An initial
  acceptance-test field typo was corrected to the existing `Order.role` field.
- Unit/targeted evidence is not yet a complete regression checkpoint. Source is
  frozen while all tests run in batches `logs/liquidity-verified-batch-0.txt`
  through `-9.txt`, each requiring VALIDATION_EXIT_CODE=0. Batch 0 covers
  unit/e2e/safety; 1–8 cover top-level integration files in sorted groups of ten;
  9 covers nested isolated Groww fixtures. ATS_TEST_BROWSER=1 enables real browser
  tests. Previous `liquidity-final-batch-*` failure logs are not passing evidence.
- Next action: inspect authoritative live process/handle and completed logs,
  finish/fix regression batches, then document and commit the verified unit locally.
  Next scope review: lot/tick-valid partial execution across declared products,
  without assuming CASH/MIS evidence proves F&O execution or Groww LIVE.

## Verified snapshot-depth conservation checkpoint (2026-09-28)

- PAPER fills now conserve each observed instrument/side book across products,
  orders, polling, cancellation and durable restart. Original snapshot evidence
  remains attached to fills; conflicting consumed-side snapshots and regressed or
  corrupt evidence fail closed. Actual historical-worker tests verify 40/80-unit
  fills and their resulting OMS exits, FIFO journal and charges.
- Resting settlement now uses sortable local broker IDs, independent of JSON map
  order. A reversed persisted-map restart test verifies scarce-depth allocation.
- Regression exposed two older fixtures updating a consumed bid book at the same
  timestamp. Exit-replacement and FIFO fixtures now explicitly advance both clock
  and quote observation for their next snapshot. Accounting assertions are intact;
  the production conservation rule was not weakened to accommodate those fixtures.
- Full backend selection: **1147 passed / 0 failed / 3 skipped** in completed
  batches, including all seven actual browser scenarios. Evidence:
  `backend/logs/liquidity-ordered-batch-0.txt` through `-4.txt`, then
  `backend/logs/liquidity-ordered-final-batch-5.txt` through `-9.txt`, each with
  `VALIDATION_EXIT_CODE=0`. Only the FIFO test fixture changed between these groups.
  This is batched full-selection coverage, not a single-process suite run.
  Three PostgreSQL-dependent checks remain skipped. Earlier failed logs are not
  passing evidence. Frontend source/bundle unchanged: prior 14 frontend tests and
  production build remain the last standalone frontend result; browser tests reran.
- EXEC-014, BT-003 and PAPER-004 remain partial pending broader lot/tick scope.
  Counts unchanged: **224 tested / 13 implemented-unverified / 97 partial /
  199 not started**. No external execution verification or M1/M2 claim.
- Exact next task: inspect and enforce lot/tick-valid partial PAPER execution using
  existing instrument metadata and boundaries; add adversarial contract fixtures,
  not a second simulator. PAPER remains default; Groww LIVE stays UNVERIFIED.

## Verified PAPER instrument-constrained fills (2026-09-28)

- Actual OMS and production broker factory now resolve existing database instrument
  metadata. PAPER orders persist lot/tick/identity/capture-time constraints; restart
  and modification retain that snapshot. Missing metadata and ambiguous legacy
  pending orders fail closed instead of filling against guessed contract rules.
- Shared fill engine enforces whole-lot quantities, including seeded partials and
  thin depth. Off-tick order/quote prices reject; adverse fallback rounds outward
  to the actual tick. Depth prices retain sub-paise precision and conserved capacity.
  Fill contract evidence reaches durable OMS trade accounting and authenticated
  workspace responses; the existing frontend consumes that API, with no new pages.
- Verified database-backed contract fixtures cover missing/invalid metadata or
  quantities, modification, restart, lot-size changes after submission, legacy
  pending orders, partial depth, sub-paise prices and adverse BUY/SELL fallbacks.
  Historical-worker fixture arithmetic now correctly yields exits 108/107.85/107.75
  and gross 888/871.35/860.25 at 0/10/20 bps with the declared 0.05 tick; this is
  deterministic synthetic test evidence, not performance evidence.
- Full backend selection: **1156 passed / 0 failed / 3 skipped**, including all
  seven browser scenarios. Authoritative complete logs:
  `backend/logs/constraints-full-batch-0.txt` through `-9.txt`, each with
  `VALIDATION_EXIT_CODE=0`. This is full-selection batched coverage, not one process.
  PostgreSQL checks remain skipped. Frontend source/build unchanged; last standalone
  result remains 14 tests and successful production build, browser checks reran.
  New constraint module/tests, updated OMS and modern adjacent tests pass Ruff;
  older engine/provider files are not claimed fully lint-clean.
- Counts remain **224 tested / 13 implemented-unverified / 97 partial / 199 not
  started**. EXEC-014, BT-003 and PAPER-004 remain partial for the remaining
  order-type lifecycle audit; low-level unconstrained simulation is explicitly not
  contract verification. Neither real derivatives nor Groww LIVE is certified.
- Exact next task: reproduce and fix resting stop-order trigger persistence across
  price reversal, partial fill and restart in the existing PAPER provider. The
  calculator currently reevaluates trigger crossing each cycle rather than
  retaining activation. Verify actual broker/OMS behavior before promoting the
  fill-model requirements. PAPER stays default; no M1/M2 completion claim.

## Verified durable stop activation / fill-model checkpoint (2026-09-28)

- Reproduced four genuine failures before changing code: BUY/SELL stop-limit
  activation without fills and partial stop-market fills lost activation after
  price reversal/restart (`backend/logs/stops-reproduction.txt`).
- Shared calculator now reports activation separately from fills. The existing
  broker persists typed instrument/price/time/source evidence before filling and
  retains it across restart and subsequent price reversal. No-fill activation is
  a durable mutation. Raw broker orders/fills expose the original activation.
- Future quotes or pre-eligibility observations cannot activate a stop. Cancellation
  prevents subsequent fills; quantity/limit modification preserves activation, but
  changing an activated trigger/type requires explicit cancel/replace. Missing
  legacy activation state rejects pending stops, corrupt evidence fails restore,
  and activation-storage failure produces no fill or account mutation.
- Twelve durable broker regression cases pass. Full backend selection:
  **1168 passed / 0 failed / 3 skipped**, including all seven actual browser
  scenarios. Complete evidence: `backend/logs/stops-full-batch-0.txt` through
  `-9.txt`, all `VALIDATION_EXIT_CODE=0`; full selection ran in separate batches.
  Three PostgreSQL checks remain skipped. New stop module/tests pass Ruff; older
  engine/provider style debt is not represented as lint-clean. Frontend source
  unchanged: last standalone 14 tests/build remain valid, browser suite reran.
- EXEC-014, BT-003 and PAPER-004 now satisfy their simulation acceptance criteria
  with broker, shared historical-worker, OMS/account/API and UI disclosure evidence
  accumulated across the verified latency/depth/contract/stop checkpoints. No new
  native-stop OMS control or live broker protection path is claimed: the reference
  worker still uses its independently monitored synthetic protection/exit service.
  Queue priority/market impact and external feed/execution remain unverified.
- Ledger recount: **227 tested / 13 implemented-unverified / 94 partial / 199 not
  started**, total 533. Also corrected the stale roll-up near the top of this file;
  historical checkpoint counts above remain historical. No M1/M2 completion claim.
- Exact next high-value application task: STRAT-012 PAPER degradation monitoring
  using actual journal/FIFO/net-cost outcomes, configured rolling thresholds,
  durable auto-disable/audit/notification behavior and existing strategy API/UI.
  Inspect current evidence/registry/worker paths first; do not create parallel
  performance accounting, fabricate missing costs, or imply LIVE validation.

## Integrated PAPER strategy degradation checkpoint (2026-09-28)

- STRAT-012 now has an actual PAPER application path: immutable versioned owner
  policies, sealed net-journal windows, shared metrics, worker and independent
  preflight enforcement, durable registry auto-disable, atomic audit/outbox notice,
  authenticated reviewed reset, typed API and existing Strategies-page controls.
  No new dashboard page, parallel accounting or fabricated trade data was added.
- Thresholds/window/origin are explicit owner configuration. No capital is guessed.
  Closed-trade drawdown is an amount, not portfolio mark-to-market. Missing costs,
  corrupt journals and unresolved corrections stand down rather than manufacture
  healthy metrics. Future closes/seals/policies are not consumed at earlier times.
  Reset requires current audit IDs and healthy evidence and never enables trading.
- Real worker/market-fixture/reference/risk/OMS/fee/FIFO/journal processing produces
  a losing trade; an actual browser verifies the resulting auto-disable and matching
  audited net drawdown. Direct preflight enforcement, unauthorized controls,
  missing/corrupt evidence, duplicate checks and atomic outbox-storage rollback are
  covered. Actual external notification delivery remains unverified.
- Full backend selection: **1176 passed / 0 failed / 3 skipped**, including **eight
  actual browser scenarios**. Complete logs:
  `backend/logs/degradation-final-batch-0.txt` through `-9.txt`, all with
  `VALIDATION_EXIT_CODE=0`. Coverage was batched, not a single process. PostgreSQL
  checks remain skipped. Frontend **16 tests passed** and production build succeeded.
  Actual OpenAPI export and generated TypeScript contract are updated. Changed
  backend modules/tests pass targeted Ruff checks.
- Regression found the monitor initially ran before the existing immutable-contract
  rejection, changing its safe error path. Moved monitoring after that validation
  and reran all batches successfully. Initial adversarial tests encountered the
  correct append-only journal guards; corruption fixtures now use isolated direct
  test-database mutations without weakening production guards.
- Counts: **227 tested / 13 implemented-unverified / 95 partial / 198 not started**.
  STRAT-012 is partial, not broadly complete: SUPERVISED/LIVE monitoring, externally
  verified evidence and broader validation remain pending. Documentation:
  `docs/PAPER_STRATEGY_MONITOR.md`. PAPER remains default; no M1/M2 claim.
- Next major integration: the advisory Claude/fallback service feeding the existing
  shared proposal-validation/sizing/risk pipeline. Inspect LLM-001 through LLM-013,
  current configuration and decision schema first; implement one source-grounded
  advisory path with schema enforcement, bounded failure and auditable telemetry,
  then connect existing API/UI state. No order authority, invented evidence, risk
  overrides, credential claims or parallel decision pipeline.

## Integrated rule-derived advisory receipt checkpoint (2026-09-28)

- The reference worker now uses the actual `DeterministicFallbackProvider` for
  its typed signal-to-proposal mapping. The existing shared validator, independent
  sizer, risk engine and preflight remain authoritative. There is no separate toy
  pipeline or fabricated research. QUANT origin and `llm_available=False` remain
  explicit; an LLM-dependent strategy cannot use this to bypass its stand-down.
- Each eligible reference signal receives a durable, redacted, versioned local
  provider receipt and hash-chained audit event before downstream validation.
  Accepted proposals atomically link the receipt with sizing/risk persistence;
  rejected candidates remain correlated by cycle. Local cost is zero, while
  remote token/temperature values are not invented. Receipt-storage failure
  prevents approval. Existing cycle claims prevent duplicate calls after restart.
- The authenticated workspace API exposes bounded, mode/time-filtered receipt
  metadata, and the existing Decisions page displays it through normal polling.
  The actual browser verifies the receipt from a real worker-generated, costed,
  closed PAPER trade. No additional dashboard page was created.
- Targeted validation initially caught a SQLite timestamp defect: storing the
  local clock directly caused the UTC point-in-time API filter to hide receipts.
  Receipt time now uses explicit UTC. All targeted and full checks reran green.
- Full backend selection: **1180 passed / 0 failed / 3 skipped**, including the
  **eight actual browser scenarios**. Logs `backend/logs/fallback-final-batch-0.txt`
  through `-9.txt` each contain `VALIDATION_EXIT_CODE=0`; complete selection is
  batched. The first batching command had an argument/encoding error and ran no
  tests; corrected batching produced these complete UTF-8 logs. PostgreSQL-only
  checks remain skipped. Frontend **16 passed**, production build succeeded,
  generated OpenAPI/TypeScript updated, and targeted Ruff checks passed.
- LLM-004/008/011/012 and AUDIT-005 are **partial**, not newly certified complete.
  Counts: **227 tested / 13 implemented-unverified / 100 partial / 193 not started**.
  Remote Claude/provider switching, paid-call budgets, circuit breaking, bounded
  remote repair and remote telemetry remain unimplemented. Configured Claude is
  not silently contacted by the quantitative reference path. See
  `docs/ADVISORY_FALLBACK.md`. PAPER remains default; Groww LIVE and external
  delivery/market verification remain unverified; no M1/M2 claim.
- Exact next integration: implement the Claude/provider abstraction and strict
  source-grounded response boundary for this existing advisory path, with durable
  daily cost/token reservations and bounded timeout/retry/circuit behavior before
  enabling paid production calls. Preserve unavailable cost as unavailable rather
  than the current local-only zero; add the necessary schema migration, fixture
  boundary tests, shared-pipeline rejection tests and existing API/UI telemetry.
  Retain deterministic fallback without falsely granting LLM availability.

## Opt-in Claude review and durable advisory controls (2026-09-28)

- The actual reference worker now supports optional source-grounded Claude review
  through the configured provider factory and real Messages HTTP adapter. Shared
  validation resolves persisted evidence before any request. The model can only
  continue or abstain with cited source IDs and advisory rationale; numeric prices,
  quantity, confidence, risk limits and permissions are not model-controlled.
  The existing shared validator/sizer/risk/preflight/OMS remains mandatory.
- Paid review defaults OFF and requires credentials plus an explicit unexpired,
  model-matched owner tariff. The initial verified request policy is Sonnet 5;
  unsupported policy/model combinations stand down to deterministic fallback.
  No external Claude or billing verification is claimed. Historical/replay origins
  prohibit remote review. LLM-dependent strategy gates are not falsely enabled.
- Atomic integer micro-USD/token reservations, provider leases and PENDING call
  receipts commit before HTTP. Known usage settles once; timeout/unknown usage or
  process interruption retains the full reservation and NULL cost. Persistent
  failure counters/cooldowns and bounded retry/re-ask prevent storms. Budget or
  provider degradation records an audit/outbox notice but permits valid QUANT
  fallback. Storage failures fail closed. Negative counters cannot admit a call.
- Requests archive the exact schema/model/output envelope without secret headers;
  redacted responses, hashes, usage, estimated cost and result are traceable to
  the proposal/cycle. Prompt instructions are pinned/versioned and separated from
  escaped untrusted data. Reference waits are capped by the worker cycle interval
  as well as the configured timeout, and quote freshness is rechecked afterward.
  Occupied PAPER lifecycle slots already skip reference evaluation, preserving
  the protection-monitoring path. No execution authority was added to the LLM.
- The existing authenticated workspace API/Decisions view exposes real receipts,
  unknown costs, and current UTC-day accounted/reserved cost/token balances.
  These are owner-tariff estimates, not provider invoices. Migration
  `0011_llm_budget` preserves unavailable costs and refuses a lossy downgrade.
  `.env.example` documents every new setting; blank tariff fields remain missing
  configuration rather than crashing boot or becoming zero-valued tariffs.
- Genuine application tests run deterministic market fixtures through ingestion,
  analysis, reference strategy, the isolated Claude HTTP boundary, shared risk,
  costed PAPER entry/protection/exit, FIFO journal and API. They prove successful
  review, abstention and timeout fallback. Additional tests cover concurrency,
  engine restart, clock rollback, circuit recovery, quota denial, strict schema,
  unsupported citations/control fields, prompt boundaries, audit-storage rollback,
  persisted-response replay and migration/downgrade behavior.
- Full batched selection: **1209 passed / 0 failed / 3 skipped** in
  `backend/logs/claude-final-batch-0.txt` through `-9.txt`, all sentinel zero.
  After the final request-archival/worker-deadline guards, the affected selection
  (including all **eight actual browser scenarios**) reran: **73 passed / 0 failed /
  3 skipped**, `backend/logs/claude-final-guards.txt`. A final equivalent exception
  cleanup and negative-counter assertion reran **41 passed / 0 failed / 0 skipped**,
  `backend/logs/claude-final-service.txt`. These overlapping selections are not
  additive and are not represented as a newer monolithic full-suite run.
  PostgreSQL checks remain skipped. Frontend **16 passed**, production build and
  generated OpenAPI/TypeScript succeeded. Changed modern modules pass targeted
  Ruff checks; pre-existing legacy config/model lint debt was not mass-reformatted.
- Tests caught Python 3.10 rejecting ISO `Z` tariff timestamps; UTC normalization
  fixes that real Windows compatibility issue. A stale-data test initially used
  two distinct fake clocks and was corrected to exercise the actual shared clock.
  The default token budget correctly blocked a second unresolved full-context
  request; the test was fixed rather than weakening that reservation. Initial
  full regression also caught undocumented new environment fields; those are now
  documented and blank-value behavior is tested.
- LLM-001 through LLM-013 remain **partial at layer scope**: the reference-review
  workload is integrated, not the general research-agent/task/schema layer. Strict
  JSON rejection/re-ask is implemented rather than arbitrary prose extraction;
  broader adversarial/provider-task parity and other model policies remain pending.
  Counts: **227 tested / 13 implemented-unverified / 109 partial / 184 not started**.
  Groww LIVE, external market/notification delivery and deployed PostgreSQL remain
  unverified. PAPER default and M1/M2 distinction are unchanged. Documentation:
  `docs/CLAUDE_ADVISORY.md` and `docs/ADVISORY_FALLBACK.md`.
- Exact next major integration: generalize declared advisory task/schema routing
  for the first source-grounded research/proposal agent, and bind LLM-origin shared
  pipeline payloads to validated persisted call receipts rather than trusting an
  availability flag alone. Preserve the working QUANT/reference review path, use
  existing stored news/fundamental/analysis sources, and implement dependency-aware
  stand-down/partial-result behavior with actual pipeline/API tests. Qualify the
  LLM ledger against complete acceptance evidence; do not jump to LIVE or fabricate
  missing research, model confidence or external verification.

### Receipt-bound proposal agent integration (2026-09-30)

- Adds an explicit opt-in `LLM_REFERENCE_TASK=proposal` to the existing paid
  reference advisory path; default remains `review`, paid calls remain disabled
  by default, and PAPER remains the only enabled execution mode. The registered
  task declares inputs, schema, prompt version and failure policy. Its proposal
  preserves quantitative prices, direction, confidence, evidence and exit rules;
  suggested quantity remains subordinate to independent sizing and risk.
- LLM-origin raw dictionaries cannot obtain approval by asserting availability.
  They require a persisted request/result receipt whose audit integrity, schema,
  raw-response hash, task, context, cycle, mode and observation freshness pass.
  Current source snapshots must equal the archived model inputs. A transactional
  one-time claim and re-verification prevent reuse and roll back failed claims.
  Receipt consumption survives engine restart. QUANT/reference fallback remains
  independent; historical/replay data never invokes the remote proposal task.
- Extends actual worker integration tests through fixture ingestion, reference
  strategy, isolated Claude HTTP, shared validation/sizing/risk, PAPER entry/fill,
  protection/exit, costed FIFO/journal and workspace API. The LLM proposal's
  suggested 9999 units becomes the actual costed 111-unit PAPER position, not an
  executable model size. Abstention and provider timeout remain covered. Existing
  lifecycle fixtures now explicitly submit quantitative signals, not unaudited
  dictionaries masquerading as model output. No production fixture bypass exists.
- Full batched regression: **1242 passed / 0 failed / 3 skipped**, with zero exit
  sentinels in `backend/logs/receipt-full-batch-0.txt` through `-9.txt`, including
  all **eight actual browser scenarios**. Interrupted batches were restarted only
  after process absence was verified; completed batches were retained. PostgreSQL
  schema checks require an external test database and remain skipped. After final
  dependency and receipt-signal identity guards, the affected unit/pipeline/agent/
  worker/strategy/PAPER selection reran: **105 passed / 0 failed / 0 skipped**,
  `backend/logs/receipt-final-guards.txt`. These selections overlap and must not be
  summed or represented as a newer full-suite run. Frontend **16 passed**, production
  build succeeded. Changed modern modules/tests pass targeted Ruff checks.
- Adversarial tests cover altered raw/parsed/request/source/audit state, wrong
  task, wrong context, future/stale receipts, transaction-time mutation, restart,
  concurrent claims, missing agent declarations and unsupported numeric output.
  Tests caught independent fixture clocks and an assertion selecting a setup-only
  proposal rather than the order-linked proposal; fixtures now test the actual
  lifecycle. Concurrent risk-ledger initialization remains fail-closed; the
  receipt race test specifically exercises the transactional receipt claim.
- AIR-001, AIR-007 and AIR-008 advance only to **partial**: initial task routing,
  stateless request reproduction and conservative numeric checks exist, but the
  broader research-agent layer and semantic grounding remain unfinished. No
  newly completed requirement is claimed. Counts: **227 tested / 13 implemented-
  unverified / 112 partial / 181 not started**. No new frontend page or endpoint
  was needed; existing Decisions/workspace state exposes the linked receipts.
- Final guards also reject a QUANT signal for an LLM-dependent strategy even when
  a caller asserts availability; the same dependency can proceed through a valid
  context-bound receipt. Receipt inputs must match the registered signal version,
  instrument, product, timeframe, origin and timestamp, not merely a context hash.
- Exact next major integration: source-anchored news/research inputs through
  registered tasks, bounded provider budgets and dependency-aware
  stand-down. Preserve news-noncritical degradation, no-lookahead, audit/outbox,
  and actual API/worker verification; never invent unavailable research.
- External Claude, provider billing, Groww LIVE, deployed PostgreSQL and real
  notification delivery remain unverified. M1/M2 completion is not claimed.

### News corroboration prerequisite (2026-09-30)

- Inspection before research assembly found that shared proposal validation
  accepted a news row's `VERIFIED` flag without actual configured-source checks.
  Stored sentiment also trusted embedded source slugs/tiers. Both now use one
  point-in-time corroboration resolver. Unknown provenance, conflicts, missing
  body, stale/future observations, disabled/unconfigured publishers, wrong HTTPS
  host attribution and malformed source/entity data cannot justify a decision.
- Each attribution resolves to an existing enabled `NewsSource`, including its
  owner-configured publisher identity, tier, source kind and configuration time.
  Two publisher aliases count once, including case-only aliases. Admission needs
  two independent configured publishers or one Tier-1 filings/regulator source.
  Article-supplied source counts and tiers cannot promote credibility. Resolved
  source metadata is frozen into the actual proposal/audit evidence snapshot;
  stored sentiment uses the same publishers and configured tiers.
- Migration `0012_news_provenance` adds nullable publisher/data-origin fields.
  Existing records are not assigned invented provenance. Tests exercise both
  the current metadata-based bootstrap and a legacy schema containing rows before
  upgrade. Populated provenance cannot be silently discarded on downgrade. Tests
  caught SQLite returning naive persisted configuration timestamps and the initial
  migration bootstrapping current model columns; normalization and compatible-column
  checks address these without replacing the schema or changing owner edits.
- Actual shared-pipeline tests reject unverifiable news before sizing/risk, while
  a valid independent quantitative proposal still proceeds. Rejection/journal
  state is visible through the authenticated workspace and journal APIs; no
  fabricated article, order or health state is returned. No new UI page or API
  endpoint is introduced in this prerequisite batch.
- Affected integration/migration/agent/PAPER selection: **127 passed / 0 failed /
  3 PostgreSQL-only skips**, `backend/logs/news-provenance-regression.txt`.
  All unit/E2E/safety tests plus news and historical bootstrap/process selections:
  **699 passed / 0 failed / 0 skipped**, `backend/logs/news-provenance-broad.txt`.
  These selections overlap and are not summed or called a new full-suite pass.
  The most recent full batched selection remains the preceding receipt checkpoint's
  **1242 passed / 3 skipped**, with its final guards separately documented above.
  Frontend is unchanged; its latest verification remains **16 passed**, build
  successful. Targeted Ruff checks pass; the existing fallback-fixture serializer
  warning is unrelated to this change.
- NEWS-005/006 are **partial**, not completed: these are actual consumer gates,
  not a completed source ingestion/verification system. Publisher identities and
  classifications remain owner-supplied configuration, not external verification;
  matching stored attribution is not semantic proof that independent publications
  corroborate the same event. Counts: **227 tested / 13 implemented-unverified /
  114 partial / 179 not started**.
- Exact next integration: authenticated/audited source configuration and bounded,
  versioned article ingestion through existing NewsSource/NewsItem abstractions;
  then source-text-anchored interpretation and worker research assembly. Implement
  source-specific polling, deduplication/entity attribution and truthful news
  degradation before claiming autonomous news-to-trade functionality. Keep news
  non-critical for independent strategies and never substitute sentiment for a
  quantitative trigger. PAPER default and all external/M1/M2 caveats remain.

### Audited news source controls checkpoint (2026-09-30)

- Added authenticated GET list/detail and PAPER-only PUT `/api/v1/news/sources`
  controls with owner identity, meaningful reason, optimistic audit version,
  atomic configuration/audit writes, and explicit invalid/unmanaged legacy state.
  Configuration includes publisher, tier, weight and enablement; it does not
  assert external publisher verification or article corroboration.
- Connected controls to the existing Market/F&O view, including loading, empty,
  stale/error states, conflict handling and persisted configuration after reload.
  Enabled sources still explicitly report acquisition `NOT_IMPLEMENTED`.
  No requests are sent to configured endpoints; public-egress/DNS/redirect safety
  must be implemented before polling. Secrets in URL credentials/query are rejected.
- Tests caught Decimal serialization differences across database reloads; audit
  comparison now validates typed policies. The actual browser caught an ambiguous
  populated-textarea accessible label, fixed with an explicit label association.
- Verification: affected integration/browser selection **66 passed / 0 failed /
  0 skipped** (`news-controls-regression.txt`); final API controls and new actual
  browser round trip **11 passed / 0 failed / 0 skipped** (`news-controls-browser.txt`).
  Broader unit/E2E/safety selection **669 passed / 0 failed / 0 skipped**
  (`news-controls-broad.txt`). Selections overlap; not a new full-suite total.
  Frontend **18 passed**, production build successful. One existing fallback
  fixture serializer warning remains. No external service verification occurred.
- NEWS-004 advances to partial only: configuration and credibility consumers exist,
  but complete ingestion and downstream weight integration remain pending.
  Counts: **227 tested / 13 implemented-unverified / 115 partial / 178 not started**.
- Exact next task: bounded versioned article ingestion through a pluggable provider
  boundary with safe public HTTPS egress, source-specific scheduling/rate limits,
  immutable article observations and truthful degraded health; then grounded
  interpretation and worker research assembly. PAPER remains default; Groww LIVE,
  real notification delivery and M1/M2 remain unverified/incomplete.

### Versioned owner article import checkpoint (2026-09-30)

- Added authenticated PAPER-only `POST /api/v1/news/sources/{slug}/articles`
  and authenticated point-in-time `GET /api/v1/news/articles/{article_id}`.
  Imports require an enabled audited source and its current optimistic version.
  Original text, declared origin, source policy/version and observation are sealed
  transactionally with the stored NewsItem. Corrections create separate records;
  retries return the original receipt even after database engine restart.
- Every import is explicitly `OWNER_IMPORT` and `UNVERIFIED`, even for primary
  sources. There is no promotion to actionable evidence, guessed entity mapping,
  sentiment or provider connectivity. Prompt-injection text remains inert data.
  Future/naive publication, wrong-host/unsafe URLs, blank text, secret-shaped text,
  disabled/changed sources and failed audits refuse the write. Point-in-time reads
  reject pre-observation/future requests and detect row/audit inconsistencies.
- The existing Market/F&O panel imports owner-supplied article JSON and displays
  only the server receipt and unverified status. The real browser test configures
  and reloads a source, enables it, imports explicit synthetic test text through
  actual APIs and sees the real persisted receipt. No new page was added.
- Review found that production clocks advance between row construction and audit
  append, unlike fixed test clocks. Observation timestamps now use the actual
  audit event time atomically; an advancing-clock regression test verifies reads.
  One initial assertion incorrectly compared equivalent ISO timezone spellings;
  it now compares parsed instants rather than their formatting.
- Final affected news integration plus all nine real-browser workflows:
  **48 passed / 0 failed / 0 skipped**, `backend/logs/news-import-final.txt`.
  Final unit/E2E/safety selection: **669 passed / 0 failed / 0 skipped**,
  `backend/logs/news-import-final-broad.txt`. Earlier overlapping selection was
  707 passed; it is not added to these results or called a new full-suite pass.
  Frontend **19 passed**, production build successful; targeted Ruff and diff
  checks pass. The existing fallback-fixture serializer warning remains.
- NEWS-001 is partial, not completed: durable manual acquisition is integrated,
  but pluggable remote RSS/API acquisition, polling/rate limiting and safe public
  HTTPS egress are absent. Version identity is not NEWS-002 semantic deduplication.
  No other requirement is promoted. Counts: **227 tested / 13 implemented-unverified /
  116 partial / 177 not started**. No external source, Groww LIVE or notification
  delivery was verified. PAPER remains default; no M1/M2 completion claim.
- Exact next task: implement bounded remote RSS/API acquisition with DNS/redirect
  egress protection and immutable observation persistence, then per-source durable
  scheduling/rate limits and truthful news degradation. Keep imported observations
  distinguishable from fetched evidence; extend this persistence path rather than
  creating parallel storage. Follow with conservative cross-source corroboration,
  grounded interpretation and worker research assembly; independent quantitative
  strategies must remain able to operate when news is unavailable.

### Bounded remote news acquisition checkpoint (2026-09-30)

- Added a real on-demand acquisition path: authenticated PAPER request → committed
  source/version-bound attempt → DNS-pinned HTTPS → RSS 2.0 or declared JSON parser
  → existing immutable observation storage → transactional audit result → existing
  Market/F&O controls. No parallel article store or fabricated news was introduced.
  `REMOTE_FEED` and `OWNER_IMPORT` remain distinct; both remain UNVERIFIED.
- The fetcher retains the original TLS hostname and Host header while connecting
  to a validated public IPv4 address, with environment proxies and redirects off.
  Mixed non-public DNS answers, multicast/reserved targets, compression, oversized
  documents and incomplete/ambiguous input fail closed. Bounds are 15 seconds,
  one megabyte and 100 articles; XML disallows DTD/entities/external references
  using the declared defusedxml dependency. Missing text/timestamps are not filled in.
- Added POST `/api/v1/news/sources/{slug}/poll` and GET
  `/api/v1/news/sources/{slug}/acquisition`. Each source has a durable cooldown
  using NEWS_POLL_INTERVAL_SECONDS, including failed, cancelled and restarted
  attempts. Concurrent changes invalidate fetched batches. Invalid articles or
  failed writes roll back the whole batch; sanitized failure receipts and explicit
  pending/degraded/stale/empty states never claim successful acquisition.
- The real browser now configures/reloads a source, imports owner fixture text,
  requests remote fixture acquisition through actual application services, and
  reloads its actual acquisition receipt. Only the network boundary is substituted;
  fixture provenance remains SYNTHETIC. The UI makes no external connectivity or
  article-corroboration claim. News failure does not modify the trading gate.
- Full backend verification in eleven disjoint batches: **1308 passed / 0 failed /
  3 PostgreSQL-only skips**, `backend/logs/news-remote-full-0.txt` through `-10.txt`.
  A first PowerShell grouping command failed before collection and was corrected;
  it is not counted as a test result. Targeted preceding acquisition/browser
  selection: **69 passed / 0 failed / 0 skipped**.
- Final review caught missing cross-origin PUT support for source controls and an
  empty-feed completion path that could otherwise backdate an audit after clock
  regression. Added explicit allowed-origin PUT preflight coverage, monotonic
  acquisition guards and in-flight source-change rejection tests. Post-fix auth,
  acquisition and all nine browser scenarios: **57 passed / 0 failed / 0 skipped**,
  `backend/logs/news-remote-final-guards.txt`. These overlap the full run and are not
  added to its total. Final unit/E2E/safety plus polling and evidence verification:
  **713 passed / 0 failed / 0 skipped**, `backend/logs/news-remote-final-broad.txt`.
  Frontend: **20 passed**, production build successful. Targeted Ruff checks pass.
- Requirements remain **227 tested / 13 implemented-unverified / 116 partial /
  177 not started**. NEWS-001 stays partial because automatic polling is not yet
  integrated. No external feed, TLS deployment, PostgreSQL deployment, Groww LIVE
  or real notification delivery is claimed verified. PAPER remains default;
  M1/M2 remain incomplete. Supported public UTF-8 RSS/JSON feeds are a bounded
  subset, not arbitrary provider APIs, Atom, authenticated feeds or filing adapters.
- Exact next task: integrate this poller into the application lifecycle with
  opt-in scheduling, bounded source iteration, restart-safe cooldowns, non-critical
  health and durable degradation notifications. Then connect point-in-time news
  availability to strategy requirements, followed by conservative corroboration,
  entity mapping and grounded interpretation. Do not promote acquisition alone
  to actionable evidence or let a news outage stop independent strategies.

### Opt-in news scheduler and durable degradation checkpoint (2026-09-30)

- FastAPI lifespan now owns the optional APScheduler news runtime. PAPER-only
  `NEWS_POLLING_ENABLED` defaults false; NEWS_ENABLED must also be true. Bounded
  source batches rotate through configured enabled sources, use existing audited
  source versions and durable per-source cooldowns, and coalesce overlapping ticks.
  The batch size defaults to five (maximum twenty). A large universe rotates over
  multiple ticks; the interval is not a guarantee every source refreshes each tick.
- Actual scheduled callbacks use the existing remote provider, parser and immutable
  persistence path. Restart cannot repeat an already admitted source poll within
  its cooldown. Shutdown cancels an in-flight acquisition and preserves its intent
  for honest degraded/recovery state. Optional scheduler startup failure reports
  UNAVAILABLE without changing the trading gate or stopping the application.
- Failed acquisition results now atomically request durable NEWS_DEGRADED
  notifications linked to the source attempt. External delivery is not claimed.
  A non-critical acquisition health check describes the current bounded batch;
  the independent news research health remains degraded. Acquired text remains
  UNVERIFIED and `actionable_news=UNAVAILABLE` until research assembly is implemented.
- Added authenticated typed GET `/api/v1/news/runtime`. The existing Market/F&O
  view reads actual scheduler status, last completed cycle and research availability;
  it clears cached runtime status on refresh failure. Source adapter capability is
  labelled SUPPORTED_ADAPTER, not incorrectly ON_DEMAND_ONLY or Connected.
- Tests exercise actual APScheduler execution, actual FastAPI lifespan/API state,
  bounded multi-source rotation, failed fetch/outbox linkage, restart cooldown,
  cancellation with a durable pending attempt, stale runtime state and non-critical
  startup failure. Only the external network boundary uses synthetic fixtures.
  Final application/news/health/notification and all nine browser scenarios:
  **68 passed / 0 failed / 0 skipped**, `backend/logs/news-scheduler-final.txt`.
  Broad unit/E2E/safety: **687 passed / 0 failed / 0 skipped**,
  `backend/logs/news-scheduler-broad.txt`. Frontend **20 passed**, build successful.
  Existing third-party deprecation and fallback serializer warnings remain.
  These are focused selections, not a new full-suite total; the preceding remote
  checkpoint's full eleven-batch result was 1308 passed and 3 PostgreSQL skips.
- NEWS-001 traceability now includes the real runtime and lifecycle tests; it stays
  partial pending consumer integration rather than presenting acquired text as
  completed research. Counts unchanged: **227 tested / 13 implemented-unverified /
  116 partial / 177 not started**. PAPER remains default; real feed deployment,
  external notification delivery and Groww LIVE remain unverified; no M1/M2 claim.
- Exact next task: connect point-in-time acquisition/research availability to
  declared strategy requirements and worker context assembly. Verify that degraded,
  stale, unverified or future news blocks news-dependent decisions while the real
  independent quantitative PAPER lifecycle still completes. Then implement
  conservative corroboration/entity mapping and source-grounded interpretation;
  acquisition or positive sentiment must never become a standalone trade trigger.

### Server-resolved news dependency gates checkpoint (2026-10-01)

- StrategyEngine now replaces caller-supplied news context with point-in-time
  server evidence whenever the strategy declares news as a required input. It
  checks configured-source corroboration, instrument attribution, provenance,
  freshness and acquisition history known at the decision cutoff. Future polling
  failures do not affect earlier decisions. Missing/unverified/stale/conflicting
  data and disabled news suppress entry evaluation with an explicit reason.
- DecisionPipeline independently enforces the same dependency and requires that
  NEWS citations refer to admitted evidence. A forged context flag or an unrelated
  valid article cannot satisfy a declared dependency. ReferenceDecisionService
  links the resolved evidence into proposal construction and preserves consumed
  news snapshots in the real reference evaluation audit.
- News-only storage failure produces NEWS_SERVICE_DEGRADED without tripping an
  unrelated global error latch; general decision/audit database failures retain
  existing fail-closed behavior. Independent quantitative paths never query news.
  The actual costed reference worker completes entry/fill/protection/exit/journal,
  restart deduplication and authenticated workspace reads with NEWS_ENABLED=false.
  The test explicitly reloads and asserts the disabled configuration rather than
  relying on an environment edit against cached settings.
- Added authenticated typed GET `/api/v1/news/research/{instrument_id}` with
  explicit cutoff/origin/age. AVAILABLE means deterministic admission, not external
  publisher/fact verification. Existing legacy verified rows retain their prior
  metadata checks; absence of polling history does not fabricate acquisition.
  Automatic imports/fetches remain UNVERIFIED until corroboration is implemented.
- Tests caught a genuine IST/UTC database-filter defect: SQLite stores these rows
  in UTC, so binding an IST cutoff excluded valid rows. Research filters now
  normalize to UTC before querying; aware instants and historical exclusions pass.
- Final news/pipeline/strategy/reference/receipt and all nine browser scenarios:
  **88 passed / 0 failed / 0 skipped**, `backend/logs/news-gates-final.txt`.
  Broad unit/E2E/safety selection: **687 passed / 0 failed / 0 skipped**,
  `backend/logs/news-gates-broad.txt`. Frontend **20 passed**, build successful;
  API types regenerated. These are focused selections, not a new full-suite total.
  The latest full eleven-batch run remains 1308 passed / 3 PostgreSQL-only skips,
  followed by the separately recorded focused checks at subsequent checkpoints.
- NEWS-012 is partial: declared-news gates and independent PAPER continuity are
  integrated, but automatic research production and sentiment-policy assembly are
  still missing. Counts: **227 tested / 13 implemented-unverified / 117 partial /
  176 not started**. No external feed, notification delivery or Groww LIVE claim;
  PAPER stays default and M1/M2 remain incomplete.
- Exact next task: conservative versioned article grouping/corroboration and
  deterministic instrument entity attribution from ingested observations, followed
  by source-grounded interpretation. Produce auditable research records consumed
  by these gates, without mutating raw observations or treating headline similarity
  as factual corroboration. Keep contradictory or ambiguous reports unavailable;
  sentiment must remain a contributing input, never a standalone trigger.

### Audited article mention attribution checkpoint (2026-10-01)

- Actual owner imports and remote acquisition now derive instrument mentions in
  the same transaction as the immutable article receipt. Matching uses configured
  exchange-qualified symbols, sufficiently specific full names and format-checked
  catalog ISINs. Bare symbols remain below threshold; ambiguous names/ISINs are
  excluded rather than arbitrarily choosing an instrument or exchange. Scores are
  deterministic rule scores, not calibrated probabilities or external validation.
- Separate hash-chained catalog snapshots and versioned attribution events retain
  exact source-text anchors and offsets, article receipt linkage and metadata
  provenance. Reads use catalog versions known at the requested instant, not the
  current mutable instrument master. Future metadata is excluded; restart reads,
  metadata corrections, idempotent re-attribution, clock regression rejection and
  transaction rollback on attribution audit failure are tested.
- Authenticated GET/POST `/api/v1/news/articles/{article_id}/entities` expose
  historical reads and explicit PAPER re-attribution. Raw observations are never
  rewritten or promoted to VERIFIED. A mention does not assert tradability:
  inactive instruments may be mentioned, and execution eligibility remains an
  independent gate. Malformed/tampered article evidence fails closed.
- Existing Market/F&O article-import view reads the real typed attribution API,
  displays anchored matches or an honest empty state, explains heuristic scores,
  and clears prior results when refresh fails. The actual browser exercises the
  production bundle and authenticated API, not a static mock dashboard.
- Final news/acquisition/research/reference-worker and all nine browser checks:
  **52 passed / 0 failed / 0 skipped**, `backend/logs/news-entities-final.txt`.
  Broad unit/E2E/safety: **687 passed / 0 failed / 0 skipped**,
  `backend/logs/news-entities-broad.txt`; existing fallback serializer warning.
  Frontend **21 passed**, production build successful; API types regenerated.
  These are focused selections, not a new full-suite total.
- NEWS-003 remains partial pending consumption by derived corroborated research.
  Counts: **227 tested / 13 implemented-unverified / 118 partial / 175 not started**.
  Full catalog scans are bounded at 25,000 equity/index entries and reject larger
  scopes rather than silently truncating. This conservative matcher does not claim
  general NLP entity extraction or semantic event interpretation. PAPER remains
  default; external feeds, notification delivery and Groww LIVE stay unverified.
- Exact next task: versioned cross-source story grouping with URL canonicalisation
  and content similarity, preserving raw article revisions and publisher identity.
  Similarity may propose a group but cannot establish factual corroboration; build
  source-grounded claim/contradiction checks before admitting derived research to
  existing strategy/proposal gates. Do not mark M1/M2 complete.

### Full regression validation after attribution (2026-10-01)

- All eleven disjoint backend batches completed: **1335 passed / 0 failed /
  3 skipped**. Evidence: `backend/logs/news-attribution-full-0.txt` through
  `backend/logs/news-attribution-full-10.txt`; every log ends with
  `VALIDATION_EXIT_CODE=0`. The skips require PostgreSQL; they are not passes.
  ATS_TEST_BROWSER=1 enabled the real React/browser integration scenarios.
- This run includes unit/E2E/safety, all top-level integration tests and the Groww
  adapter's isolated fixture tests. It confirms no detected suite regressions,
  not external Groww connectivity, live orders, deployment verification or M1/M2.
  Frontend remains 21 passing tests and a successful production build.
- Counts and next task unchanged: 227 tested / 13 implemented-unverified /
  118 partial / 175 not started; continue auditable cross-source story grouping
  and grounded corroboration without promoting similar headlines to facts.

### Versioned cross-source story grouping checkpoint (2026-10-01)

- Production owner imports and remote acquisition now create an auditable story
  group in the same transaction as raw observation and instrument attribution.
  Three same-story source observations resolve to one derived group listing all
  three source/publisher identities; all original article revisions remain intact.
- The versioned deterministic grouping rule uses conservative HTTPS URL
  canonicalisation, title token similarity, exact normalized text or high ordered
  bigram similarity. URL revisions still require similar titles. Every candidate
  must match every member, preventing transitive similarity bridges; a candidate
  matching multiple groups stays separate with an explicit ambiguity flag.
- Groups preserve article and source audit IDs, publication/observation timestamps,
  canonical URLs, publisher identities and origin. Different origins never merge.
  Membership and group revisions are hash-chained; a transactional audit cursor
  rejects concurrent group changes instead of allowing silent lost updates.
  Group history and raw member integrity are checked by authenticated typed GET
  `/api/v1/news/articles/{article_id}/story`, including historical cutoff reads.
- Existing Market/F&O view displays actual group/source state and explicitly says
  similarity is not factual corroboration. Empty historical membership returns
  unavailable, refresh failures clear prior results, and all groups remain
  UNVERIFIED. Repeated publishers or syndicated text do not become independent
  factual support merely by appearing in a group. No trade gate is weakened.
- Tests exercise three-source grouping, restart/history, source tampering, origin
  separation, similarity bridges, ambiguous matches, future reads, clock rollback,
  atomic rollback on membership-audit failure and the production React/API flow.
  Final selection including all nine real browser scenarios: **59 passed / 0
  failed / 0 skipped**, `backend/logs/news-grouping-final.txt`. Broad unit/E2E/safety:
  **687 passed / 0 failed / 0 skipped**, `backend/logs/news-grouping-broad.txt`.
  Frontend **22 passed**, production build successful; typed contract regenerated.
  The preceding complete suite remains 1335 passed / 3 PostgreSQL skips; focused
  selections here are not a new full-suite total. Existing serializer warning only.
- NEWS-002 remains partial: grouping is integrated, but corroborated research
  consumption and semantic contradiction checks are still pending. Historical
  imports made before this implementation are not silently backfilled. Matching
  uses a 24-hour publication/observation window, at most 1000 recent group snapshots
  and 20 members per group; oversized scopes reject rather than truncate. These
  bounds and heuristic matching are not a general semantic news deduplicator.
- Counts: **227 tested / 13 implemented-unverified / 119 partial / 174 not started**.
  PAPER stays default, Groww LIVE and external source/delivery verification remain
  unverified, and M1/M2 remain incomplete.
- Exact next task: source-grounded structured claims/interpretation and explicit
  contradiction handling over these immutable grouped observations. Retain claim
  anchors, source identity and known-at times; reject unsupported claims and prevent
  similar/syndicated reports from automatically authorizing a trade. Integrate
  admitted research with existing strategy/proposal gates and sentiment consumers.

### Grounded advisory news interpretation checkpoint (2026-10-01)

- Added a registered, versioned news-extraction agent using the existing
  LLMProvider/Claude HTTP boundary, prompt data envelope, durable budget reservation,
  circuit/lease controls, token/cost accounting and bounded repair policy. The shared
  settlement service now accepts an internal event type so news results are not
  mislabeled as trade proposals; proposal behavior remains unchanged.
- Authenticated PAPER POST `/api/v1/news/articles/{article_id}/interpretation`
  requires the current story audit version and resolves all source text and allowed
  instruments server-side. Historical/REPLAY remote interpretation is refused.
  Inputs are bounded, source/audit-linked and stored before the HTTP call; provider
  failure or missing configuration produces UNAVAILABLE, never invented research.
- Strict schema covers event type, mapped instruments, direction, magnitude,
  confidence, horizon, extractive summary and exact attributed source quotations.
  Unknown source IDs, incorrect offsets, fabricated quotations/summaries and
  unmapped instrument IDs are dropped and explicitly audited as UNSOURCED_CLAIM.
  Duplicate JSON keys, nonfinite numbers and extra control fields fail validation.
  Quoted instructions remain data; no tool, broker or account-control capability
  is exposed to the news agent.
- Saved outcomes have historical GET reads, call-input/result audit binding and
  raw-source integrity checks. Restart retains results and costs. Later group
  revisions are shown as stale; stale owner requests are rejected. The existing
  Market/F&O view reads/generates actual API outcomes and distinguishes unavailable,
  error, stale and GROUNDED_UNVERIFIED states without creating another page.
- Grounding here proves quotation provenance, NOT semantic correctness, independent
  corroboration, factual truth or model-annotation validity. Direction/event labels
  remain unverified model inferences; confidence is explicitly an uncalibrated
  self-report, not a probability. All outcomes remain UNVERIFIED and cannot satisfy
  news-dependent trade justification. Summary prose cannot drive execution.
- Final news/LLM/proposal/PAPER and all nine real-browser checks: **107 passed /
  0 failed / 0 skipped**, `backend/logs/news-interpretation-final.txt`. Broad
  unit/E2E/safety: **687 passed / 0 failed / 0 skipped**,
  `backend/logs/news-interpretation-broad.txt`; existing fallback serializer warning.
  Frontend **23 passed**, production build successful; API contract regenerated.
  These focused checks are not a new full-suite total (last complete run: 1335
  passed / 3 PostgreSQL skips before grouping/interpretation).
- NEWS-007 and NEWS-009 are partial: owner-triggered extraction, quotation guards,
  persistence and UI are integrated, but automatic orchestration, semantic claim
  admission, contradiction handling and verified sentiment consumption are pending.
  Counts: **227 tested / 13 implemented-unverified / 121 partial / 172 not started**.
  Claude transport is tested with isolated HTTP fixtures, not real credentials;
  external sources/delivery and Groww LIVE remain unverified. PAPER stays default;
  no profitability or M1/M2 completion claim.
- Exact next task: explicit per-source claim/contradiction evidence over grouped
  observations, without treating a matching quotation or shared headline as truth.
  Suppress conflicting/ambiguous research; then connect admitted evidence and
  confidence-aware sentiment to the existing declared-news strategy gates. Keep
  independent quantitative PAPER operation available during news degradation.

### Audited source-opposition suppression checkpoint (2026-10-01)

- Actual ingestion now assesses each new story-group version transactionally and
  persists a separate hash-chained conflict assessment. The versioned conservative
  English lexical rule finds otherwise matching statements with explicit not/never
  opposition, retaining both exact quotations, source/publisher/article audit IDs,
  field offsets and known-at time. Reports remain separate; they are not averaged.
- Recognized opposing reports produce CONFLICTING with actionable=false. The real
  interpretation service rejects these before provider selection, budget reservation
  or paid calls. All other results say UNASSESSED, never verified agreement. Hedges,
  double negation, ambiguous and unsupported language do not become positive evidence.
  This is a lexical safety warning, NOT general semantic contradiction detection.
- Authenticated GET `/api/v1/news/articles/{article_id}/conflicts` checks group
  version, audit integrity and original source rows at the requested timestamp.
  Restart/historical reads retain the prior assessment; source tampering fails
  closed. Conflict-audit failure rolls back the entire new article/group transaction.
  The existing Market/F&O interpretation panel shows both statements and their
  publishers, unavailable/error states and stale group-version warnings.
- Integration tests prove opposing source fixtures suppress interpretation with
  zero LLM calls while original observations remain non-actionable. Independent
  PAPER worker and all nine production-bundle browser scenarios remain passing.
  Selected final regressions: **110 passed / 0 failed / 0 skipped**,
  `backend/logs/news-conflicts-final.txt`; broad unit/E2E/safety: **687 passed / 0
  failed / 0 skipped**, `backend/logs/news-conflicts-broad.txt`. A final newline
  sentence-boundary correction then passed **35 news ingestion/grouping/interpretation
  checks**, `backend/logs/news-conflicts-boundaries.txt`. Frontend **24 passed**,
  production build successful; API types regenerated. These selections are not a
  new full-suite count. Existing fallback serializer warning remains.
- NEWS-008 is partial: lexical opposition is integrated and blocking, but wider
  semantic contradictions, numeric discrepancies, same-source internal conflicts
  and language variants remain unassessed. Scope is bounded to 200 candidate
  statements per field and 100 opposition pairs; overflow fails closed rather than
  truncating. Legacy groups without assessments remain explicitly unavailable.
- Counts: **227 tested / 13 implemented-unverified / 122 partial / 171 not started**.
  PAPER stays default. Real Claude/source/delivery verification and Groww LIVE
  remain unverified. No profitability, complete news verification or M1/M2 claim.
- Exact next task: durable derived research admission distinct from immutable raw
  news, with explicit source-policy corroboration, instrument attribution, claim
  provenance, recency and conflict blockers. Integrate that resolver into the shared
  NEWS evidence/strategy gates and sentiment consumption; never treat unflagged
  lexical checks or model confidence as proof of factual agreement.

### Shared news-consumer readiness correction (2026-10-01)

- Before extending derived research admission, inspection found an actual safety
  gap: acquisition-health checks applied to declared-news strategy context, but
  direct NEWS citations from otherwise independent strategies and stored sentiment
  could still consume legacy verified items after source acquisition degraded.
- Moved the point-in-time acquisition check into shared news verification. Direct
  proposal validation, sentiment and research now enforce the same disabled,
  pending, degraded, empty and stale-source refusal. The configured news age window
  also bounds every consumer's requested window. Research no longer duplicates
  readiness logic, and future polling failures cannot poison historical evidence.
- Regression tests use the actual shared pipeline: unhealthy news citations reject
  with zero approved quantity, sentiment has no usable contribution, and the same
  independent quantitative proposal without news still reaches RISK_APPROVED.
  Initial new-test failures exposed a missing fixture clock setup, corrected by
  using the existing pipeline time-control fixture; no production gate was relaxed.
  Focused news verification/availability/sentiment checks: **40 passed / 0 failed**.
- Full backend regression, twelve disjoint batches with real browsers enabled:
  **1361 passed / 0 failed / 3 PostgreSQL-only skips**. Evidence:
  `backend/logs/news-consumer-full-0.txt` through
  `backend/logs/news-consumer-full-11.txt`, all ending in VALIDATION_EXIT_CODE=0.
  Existing serializer/third-party deprecation warnings remain. Frontend source is
  unchanged in this fix; its latest checkpoint has 24 passing tests and a successful
  production build, exercised again by this run's real-browser tests.
- NEWS-005/006 traceability updated; statuses/counts unchanged: **227 tested /
  13 implemented-unverified / 122 partial / 171 not started**. This closes a consumer
  consistency defect, not the remaining derived-research admission work. Legacy
  data without polling history retains its prior provenance rules; no acquisition
  receipt or external verification is invented. PAPER remains default; Groww LIVE,
  actual external feeds/Claude/delivery and M1/M2 remain unverified/incomplete.
- Exact next task remains durable derived-research admission distinct from raw
  observations. Reuse this common readiness gate while enforcing source-policy
  corroboration, claim provenance, instrument attribution, recency and conflicts;
  connect admitted snapshots to NEWS citations, declared strategy dependencies and
  sentiment without upgrading mere headline similarity or model scores to facts.

### Durable quotation research admission checkpoint (2026-10-01)

- Added immutable, idempotent research-admission audit snapshots distinct from raw
  news. Authenticated PAPER POST `/api/v1/news/articles/{article_id}/research`
  resolves a current grounded interpretation, group/conflict version, exact quote
  anchors and instrument attribution inside each quotation. Model-selected IDs,
  generic headlines and mere group membership cannot confer instrument relevance.
- A quotation requires a configured Tier-1 primary source or a supporting pair with
  distinct configured publishers, domains AND full-body fingerprints. Those three
  independence checks must hold for the same pair, not separately across different
  records. Duplicate publishers, same-host aliases, exact syndicated copies,
  unsupported entities and interpretation outputs with dropped claims stand down.
  These are conservative source-policy criteria, not externally verified publisher
  ownership, semantic fact checking or a guarantee against paraphrased syndication.
- Every consumption rechecks source/configuration/audit versions, acquisition
  readiness, provenance, recency, conflict state, source integrity and the exact
  admitted snapshot. Source changes invalidate current use while historical reads
  retain the policy known at their cutoff. Future admissions cannot justify earlier
  decisions; restart preserves receipts. Failed admission audit rolls back cleanly.
- Both ProposalValidator NEWS resolution and server-side declared-news strategy
  research now consume these durable IDs. Actual imported/attributed source text,
  real budgeted interpretation with only HTTP fixtures, admission and the shared
  validation/sizing/risk pipeline reach RISK_APPROVED in integration tests. Raw
  NewsItem rows stay UNVERIFIED and are never rewritten to manufacture acceptance.
- Existing Market/F&O controls evaluate admission against the displayed server
  interpretation. A new real-browser scenario performs that action and the backend
  then consumes its admitted evidence through the real decision/risk pipeline.
  Scheduler wording now distinguishes unavailable automatic research production
  from per-instrument admitted quotations, avoiding a false global availability label.
- SOURCE_POLICY_ADMITTED explicitly means quotation/source-policy admission, not
  external truth verification. It authorizes neither an order nor a standalone
  signal. Direction labels/model confidence are NOT promoted with quotations;
  sentiment_score and interpretation remain unavailable in admitted snapshots until
  the separate advisory sentiment policy is implemented. No numeric sentiment is
  invented to make this workflow look complete.
- Integrated news/LLM/strategy/risk/PAPER and ten browser scenarios: **146 passed /
  0 failed / 0 skipped**, `backend/logs/news-admission-final.txt`. Broad unit/E2E/
  safety: **687 passed / 0 failed / 0 skipped**, `backend/logs/news-admission-broad.txt`.
  Final rebuilt dashboard/admission/browser selection: **18 passed / 0 failed /
  0 skipped**, `backend/logs/news-admission-browser-final.txt`. Frontend **25 passed**,
  production build successful; typed API regenerated. These overlapping selections
  are not a full-suite sum; last complete suite remains 1361 passed / 3 PostgreSQL
  skips before this checkpoint. Existing fallback serializer warning remains.
- NEWS-005 traceability updated but remains partial pending automatic production
  and broader corroboration coverage. Counts unchanged: **227 tested / 13
  implemented-unverified / 122 partial / 171 not started**. Ten existing application
  views are preserved; no mock dashboard/new page or extra LLM provider was added.
  Real external feeds/Claude/delivery and Groww LIVE remain unverified; PAPER is
  default and M1/M2 remain incomplete.
- Exact next task: add a versioned, explicitly advisory sentiment policy for admitted
  quotation evidence, preserving uncalibrated model-confidence labeling and the
  no-standalone-trigger rule. Then opt-in scheduled research generation/admission
  through the existing bounded LLM budget/runtime, with unavailable/degraded states
  and restart-safe deduplication rather than repeated paid calls or invented scores.

### Integrated advisory sentiment checkpoint (2026-10-02)

- Added a versioned, hash-audited owner policy for optional scoring of admitted
  quotations. Enabling requires explicit acceptance of uncalibrated model scores;
  absent/disabled policy is UNAVAILABLE. No model label becomes verified truth.
- Reuses the existing aggregator with versioned credibility/source-weight/model
  confidence/recency weighting. Direction UNKNOWN and low confidence are excluded,
  not replaced with neutral zero. Exact admitted claim/instrument scope, origin,
  source readiness, historical cutoff and recency are rechecked on consumption.
- Strategies declaring both news and sentiment receive server-computed evidence,
  not caller-supplied scores. The shared pipeline independently enforces policy and
  requires citations for every consumed admission; proposal/decision/reference
  audits retain the policy version and full contribution snapshot. Sentiment-only
  triggers remain prohibited and quantitative-only strategies remain unaffected.
- Authenticated GET/PUT `/api/v1/news/sentiment-policy` and GET
  `/api/v1/news/sentiment/{instrument_id}` expose actual state. The existing Market
  view supports policy configuration and evidence inspection, with explicit origin,
  unavailable/error states and uncalibrated-advisory warnings. No new page or mock
  data was added. Policy writes remain PAPER-only and optimistic/version-checked.
- Tests demonstrate hand-calculated weighted score 1/3, restart reproducibility,
  historical policy disable, future/stale/wrong-origin exclusion, explicit consent,
  forged-context rejection and missing contribution citation rejection. Real Edge
  browser performs admission, configures policy and reads actual API scoring.
- Broad unit/E2E/safety plus focused integrations: **720 passed / 0 failed / 1
  browser-opt-in skip**, `backend/logs/advisory-sentiment-validation.txt`. Rebuilt
  actual-browser/admission/sentiment selection: **15 passed / 0 failed / 0 skipped**,
  `backend/logs/advisory-sentiment-browser.txt`. Final sentiment selection **6 passed**.
  Frontend **26 passed**, production build successful and API types regenerated.
  These overlap and are not a full-suite sum. Existing serializer warning remains.
  Testing caught and fixed an inaccessible dropdown label; stale-data testing uses
  the service after the short-lived authentication fixture expires.
- SENT-001 remains partial: advisory labels are not calibrated/fact-verified and
  automatic research production is still pending. Counts unchanged: **227 tested /
  13 implemented-unverified / 122 partial / 171 not started**. External feeds/Claude,
  delivery and Groww LIVE remain unverified; PAPER default and M1/M2 incomplete.
- Exact next task: opt-in scheduled research generation/admission using the existing
  runtime and bounded LLM budget, with durable pre-call deduplication, no automatic
  replay of ambiguous paid calls after restart, and explicit unavailable/degraded
  states. Run a complete regression checkpoint alongside that integration.

### Complete advisory regression verification (2026-10-02)

- Verified local implementation checkpoint `6c063af` against the complete backend
  suite: **1376 passed / 0 failed / 3 PostgreSQL-only skips**. Twelve disjoint batches
  cover unit/E2E/safety, every top-level integration file, and isolated Groww adapter
  tests. Real browser scenarios were enabled. Evidence:
  `backend/logs/advisory-full-0.txt` through `advisory-full-11.txt`, each with exit 0.
- Frontend remains **26 passed**, production build successful. No external service,
  deployed PostgreSQL or Groww LIVE verification is implied. No requirement status
  promotion: **227 tested / 13 implemented-unverified / 122 partial / 171 not started**.
- Next remains opt-in scheduled research generation/admission with durable pre-call
  deduplication and fail-closed restart handling; advisory sentiment is complete as
  this bounded integration, not complete automatic news research or M1/M2.

### Opt-in scheduled research production checkpoint (2026-10-02)

- Connected the existing acquisition runtime to a bounded producer over stored
  observations. `NEWS_RESEARCH_ENABLED=false` remains the default; enabling it also
  requires the existing news/acquisition scheduler opt-ins and PAPER mode. The
  application selects LIVE observations; isolated tests explicitly inject SYNTHETIC
  providers without relabeling them. No new trading pipeline or broker path exists.
- Reuses source readiness, immutable grouping/entity/conflict evidence, the registered
  NEWS_AGENT task, Claude provider and shared durable budget/circuit controls. Each
  group version receives a committed production intent before any paid request.
  Duplicate/concurrent/restarted workers cannot replay an ambiguous request. Saved
  matching interpretations can finish admission without another call; unknown
  outcomes report RECOVERY_REQUIRED and retain their audit/budget evidence.
- Admission receipts and final production outcome commit together. Tests inject a
  final audit failure, confirm admission rollback, restart and recover the saved
  interpretation with exactly one HTTP call. Intent persistence failure makes no
  provider call. Missing credentials produce durable UNAVAILABLE, never fake research.
- Actual fixture HTTP feeds traverse the real fetcher/parser/poller, observation
  persistence, attribution/grouping, production, grounded interpretation, source-
  policy admission and shared validation/sizing/risk decision. Separate tests invoke
  the actual APScheduler callback. Only external HTTP/DNS boundaries are fixtures.
  Raw observations stay UNVERIFIED; model labels remain advisory and uncalibrated.
- Existing authenticated `/api/v1/news/runtime` and Market view expose bounded
  research-batch outcomes, admission IDs and recovery-required states. No new page.
  PER_INSTRUMENT_CHECK_REQUIRED is explicitly not a global availability certificate;
  strategy/decision consumers still recheck every citation at their decision cutoff.
- Integrated news/shared-decision/PAPER/browser selection: **132 passed / 0 failed /
  0 skipped**, `backend/logs/news-production-integrated.txt`. Broad unit/E2E/safety:
  **687 passed / 0 failed / 0 skipped**, `backend/logs/news-production-broad.txt`.
  Frontend **27 passed**, production build successful, API types regenerated. Latest
  complete pre-producer regression remains **1376 passed / 3 PostgreSQL skips** at
  `0afdd55`; focused selections are not reported as a new full-suite total.
- AIR-002 corrected from not-started to partial with actual implementation/test
  links; NEWS-007 traceability expanded. Counts: **227 tested / 13 implemented-
  unverified / 123 partial / 170 not started**. No requirement was promoted to tested
  merely because a new test exists. PAPER default; real feeds/Claude/billing/delivery,
  deployed PostgreSQL and Groww LIVE remain unverified. M1/M2 remain incomplete.
- Limitations: terminal unavailable jobs do not automatically retry unchanged groups;
  owners can use the existing authenticated interpretation/admission controls. There
  is no dedicated audited retry/abandon control or external billing reconciliation.
  Scans rotate bounded candidates and coalesce; they do not promise full-universe
  research every tick. Unsupported filing feed formats remain manual-import only.
- Exact next task: authenticated, audited production-job recovery controls and
  explicit bounded retry authorization for terminal failures, without automatic
  replay of pending/ambiguous paid calls or deleting audit records. Then integrate
  NEWS-011 instrument-specific event alerts/entry stand-down with the existing
  deterministic gates; do not let an advisory score independently trigger a trade.

### Authenticated research-job recovery checkpoint (2026-10-02)

- Added authenticated GET `/api/v1/news/production/{group_event_id}` and POST
  `/api/v1/news/production/{group_event_id}/control`, integrated into the existing
  Market view. Controls require the current audit head and a meaningful reason,
  record the authenticated actor, and remain PAPER-only. Loading/error/stale-write
  handling never presents a failed mutation as success or enables another action
  against an unconfirmed version.
- Production history now replays attempts, recovery requests, bounded retry
  authorizations and abandonment without rewriting the original intent/outcome.
  Existing two-event job histories remain supported. RECOVER consumes a saved
  interpretation only; it cannot invoke Claude. ABANDON prevents later automatic
  admission, including an already-in-flight response, while retaining provider
  receipts and conservative budget reservations. It does not unsend requests,
  refund costs, revoke previous evidence or change any trading risk control.
- Retry is explicit, single-use and restricted to proven no-call terminal failures
  with the corresponding current interpretation receipt. Pending work, timeouts
  and ambiguous paid failures are rejected. Owner retry count defaults to one per
  group version, configurable from zero to three; authorizations expire within an
  explicit 1–900 second window. Source readiness, scheduler opt-in and all original
  budget/risk boundaries remain authoritative. An authorization expiring while
  its claim is written rolls back before any paid request.
- Tests cover actual browser authorization consumed by the producer, restart,
  duplicate/concurrent authorization, cancellation during a paid call, preserved
  unknown reservations, late response after abandonment, saved-receipt recovery,
  audit rollback, timeout rejection and expiry during claim persistence. Only
  external HTTP boundaries are isolated fixtures, not fabricated production data.
- Integrated news/shared decision/PAPER/browser selection: **142 passed / 0 failed /
  0 skipped**, `backend/logs/news-recovery-integrated.txt`. Broad unit/E2E/safety:
  **687 passed / 0 failed / 0 skipped**, `backend/logs/news-recovery-broad.txt`.
  Frontend **28 passed**, production build successful, API types regenerated;
  changed backend modules pass Ruff. Existing fallback serializer warning remains.
  Latest complete full-suite checkpoint remains 1376 passed / 3 PostgreSQL-only
  skips at `0afdd55`, before production/recovery additions; focused results are
  not presented as a new full-suite total.
- AIR-002 traceability and stale SENT-001 wording updated without status promotion.
  Counts unchanged: **227 tested / 13 implemented-unverified / 123 partial / 170
  not started**. External billing reconciliation, actual feeds/Claude/delivery,
  deployed PostgreSQL and Groww LIVE remain unverified. PAPER remains default;
  M1/M2 are not complete. Paid-response retries beyond the no-call allowlist remain
  unavailable through this control; manual interpretation is a separate explicit
  owner action, not automatic replay or permission to clear unknown reservations.
- Exact next task: NEWS-011 source-grounded high-severity negative-event alerts
  and durable instrument-specific entry stand-down. Integrate server-resolved
  blocks with the existing deterministic pipeline AND execution preflight, expose
  them in API/UI, preserve exits and independent instruments, and test restart,
  stale/future/unadmitted evidence, duplicate notification and no bypass via caller
  `news_halt=false`. Existing RISK-014 currently tests supplied flags; verify its
  integrated acceptance and correct the ledger if required before promoting it.

### News-backed PAPER execution verification (2026-10-02)

- Inspection before NEWS-011 found a real integration defect: approvals containing
  the advisory `news_sentiment` audit sidecar could not pass the strict decision
  context decoder used by entry, monitoring, recovery and close accounting. Added
  one shared strict snapshot decoder for the known sidecars; unknown fields still
  fail validation and the original evidence remains archived unchanged.
- Execution now re-resolves NEWS citations before creating an order and again at
  final dispatch. Disabled/stale/changed sources and unavailable or changed sentiment
  policy/contributors cannot reuse an older approval. Independent quantitative
  strategies retain no news dependency; protective exits do not require news.
- A real-service integration test now follows imported/grounded/admitted quotations,
  advisory policy, shared proposal/sizing/risk approval, multi-fill PAPER entry,
  protection, database/engine restart, exit after source disable, costed FIFO,
  journal and authenticated workspace state. Its explicitly synthetic tariff yields
  independently expected gross 800, charges 54.54 and net 745.46; these are fixture
  arithmetic, not market performance or real brokerage verification.
- Negative tests reject changed source/policy and stale sentiment without broker
  submission, while an independent proposal still executes. A source disabled after
  preflight is caught again before dispatch; its unsubmitted CREATED order is not
  blindly resubmitted. Existing recovery/hygiene remains responsible for disposing
  such unsubmitted intents; this checkpoint does not pretend they were broker fills.
- Focused news/execution/cost/FIFO/reference/browser integration: **53 passed /
  0 failed / 0 skipped**, `backend/logs/news-execution-integrated.txt`. Frontend
  **28 passed**, production build successful. Complete backend regression:
  **1398 passed / 0 failed / 3 PostgreSQL-only skips**, in twelve disjoint
  `backend/logs/news-execution-full-0.txt` through `news-execution-full-11.txt`
  batches, all with exit 0. All real browser scenarios were enabled; Groww tests
  remain isolated adapter fixtures, not live verification. Existing serializer
  warnings remain. The focused selection overlaps and is not added to this total.
- Independently corrected RISK-014 from tested to partial: its tests prove named
  supplied-flag rejection, not every server-resolved ban/news/manual-block lifecycle.
  Counts now **226 tested / 13 implemented-unverified / 124 partial / 170 not started**.
  NEWS-011 is still not started; evidence revalidation is not a durable news halt.
- Exact next task remains source-grounded NEWS-011 alerts and durable per-instrument
  entry blocks, enforced in shared risk and final dispatch without suppressing exits
  or unrelated instruments. Keep typed API/UI evidence, explicit audited release,
  restart/no-lookahead/duplicate-notification coverage and rejection of caller
  `news_halt=false` bypasses. PAPER default; Groww LIVE and external dependencies
  remain unverified. No M1/M2 completion claim.

### Durable instrument news entry inhibition (2026-10-02)

- Implemented an explicit owner-enabled reaction policy with no assumed confidence
  threshold or consent. Current admitted quotation evidence must have exactly the
  held instrument's scope, NEGATIVE direction, HIGH magnitude and the owner's
  minimum confidence. These are uncalibrated model labels, not verified facts.
- Manual and scheduled research admission now share the admission/reaction
  transaction. The existing PAPER worker also evaluates admissions against current
  positions. A qualifying event archives policy, admission, quotations, interpretation
  and held-position IDs in an instrument/origin-specific durable halt, with a
  notification outbox request committed atomically. No remote delivery is claimed.
- Shared proposal processing records NEWS_HALT negative decisions. Entry freshness,
  approval safety and final dispatch independently enforce stored halt state, so
  caller `news_halt=false` cannot override it. The halt does not add a global
  engine-error latch, suppress protective exits or affect unrelated instruments.
- Restart retains the halt. Position closure, source ageing and disabling the
  reaction policy do not release it. Authenticated release requires expected head,
  explicit confirmation and reason; it appends history and retains acknowledged
  admissions to prevent repeated alerts/re-latching from the same evidence.
- Added typed policy/read/release APIs under `/api/v1/risk`, generated API types,
  and a polling panel on the existing Risk page with evidence, unavailable states
  and authenticated review controls. A real Edge browser exercises persisted
  halt display and release through the production React bundle.
- Focused integration/browser verification: **9 passed / 0 failed / 0 skipped**,
  `backend/logs/news-halts-focused.txt`. Frontend **31 passed / 0 failed**, production
  build successful; changed backend modules pass Ruff. Complete backend regression:
  **1407 passed / 0 failed / 3 PostgreSQL-only skips**, in twelve disjoint
  `backend/logs/news-halts-full-0.txt` through `news-halts-full-11.txt` batches,
  each exit 0. Browser scenarios were enabled. The interrupted runner was verified
  stopped and only its unfinished batches resumed, not counted twice. Final
  rebuilt-browser/integration recheck: **9 passed / 0 failed / 0 skipped**, in
  `backend/logs/news-halts-final.txt`; this overlaps the full-suite total.
- Review caught and fixed a real UI race before committing: polling a changed
  halt now invalidates typed review confirmation. Polling no longer silently
  advances the version used by an in-progress policy edit. Stale policy writes
  retain their reviewed version and are rejected rather than overwriting new
  owner changes. A frontend regression test verifies both behaviours.
- Test development exposed existing account-slot and missing-tariff entry blockers,
  plus token expiry after a long simulated clock advance. Tests were corrected to
  exercise the intended boundaries and preserve those controls, not bypass them.
- NEWS-011 moves from not started to **partial**, not tested-complete: current CASH
  integration is verified, but wider adversarial/concurrency coverage and future
  derivative/underlying-family integration remain. RISK-014 stays partial. Corrected
  the stale top-level roll-up: **226 tested / 13 implemented-unverified / 125 partial /
  169 not started**. See `docs/NEWS_HALTS.md` for operator contract and limitations.
- Exact next task: strengthen this same integrated halt path with transactional
  notification-failure/rollback, concurrent review/admission, new-evidence re-latch,
  unadmitted/wrong-origin/future input and actual worker-cycle adversarial tests;
  then complete remaining server-resolved instrument block sources for RISK-014.
  PAPER remains default; Groww LIVE, external feeds/Claude/delivery and deployed
  PostgreSQL remain unverified. No M1/M2 completion claim.

### News-halt failure and actual worker verification (2026-10-02)

- Extended the same application path rather than adding a parallel simulation:
  the real reference worker ingests fixture data, enters through its shared
  decision/PAPER execution pipeline, discovers previously admitted negative news
  against its new holding, and still completes its protected costed exit. The halt
  remains active after the position closes; the worker does not fail globally.
- Fault injection at notification persistence verifies atomic rollback of the
  admission, halt and outbox request. Explicit retry commits them once. An
  interpretation without admission does not trigger the halt. A changed admission
  invalidates an older owner's release request; acknowledged evidence stays quiet,
  while a distinct admission version can re-latch after release.
- Future publications are refused, admitted observations from another data origin
  do not affect the current holding, and audit tampering yields an audited shared
  pipeline rejection plus the existing risk-error latch, not another broker order.
  The historical-interpretation no-lookahead restriction remains intact. A LIVE
  origin tag in an isolated fixture is not live source or provider verification.
- Selected integrated news/worker/risk/browser regression: **72 passed / 0 failed /
  0 skipped**, `backend/logs/news-halt-recovery-integrated.txt`. Broad unit/E2E/safety:
  **687 passed / 0 failed / 0 skipped**, `backend/logs/news-halt-recovery-broad.txt`.
  These selections overlap the preceding complete **1407 passed / 3 PostgreSQL-only
  skips** checkpoint and are not added to it as a new full-suite total. Frontend
  remains unchanged at **31 passed** and a successful production build; the actual
  news-halt browser test ran again in the integrated selection. Changed tests pass Ruff.
- NEWS-011/RISK-014 traceability is updated without status promotion. Counts remain
  **226 tested / 13 implemented-unverified / 125 partial / 169 not started**.
  Cross-process/PostgreSQL concurrency, derivative-family handling and real remote
  delivery remain unverified/pending. PAPER default; Groww LIVE unverified; no M1/M2 claim.
- Exact next task: complete the remaining server-resolved RISK-014 entry controls,
  starting with durable authenticated/audited manual instrument blocks and current
  restricted-instrument evidence. Enforce in both shared decisions and final entry
  dispatch, expose state/review in the existing Risk UI, preserve exits, and prove
  caller false flags cannot bypass them. EXCH-008 F&O ban-period sourcing remains a
  separate explicit gap; do not pretend a manual block is verified exchange ban data.

### Server-held manual and catalog instrument vetoes (2026-10-02)

- Added authenticated, versioned manual entry blocks for exact canonical instrument
  IDs across all PAPER data origins. Strict boolean input, explicit block/unblock
  confirmation and meaningful review reason are required. Audit transitions retain
  actor, reviewed predecessor and catalog snapshot; a new block atomically queues
  an alert request. Release never erases history or clears other safety sources.
- Shared decisions, entry freshness/preflight and transactional/final dispatch now
  independently read the server-held manual state. Caller false flags cannot bypass
  it. Current catalog restricted/inactive flags are also rechecked after approval;
  manual release cannot override them. Named vetoes are not global engine errors.
  Existing protective exits remain enabled, including after restart and catalog
  changes. These current catalog flags are not verified exchange ban-period data.
- Added typed list/read/change APIs at `/api/v1/risk/instrument-blocks`, regenerated
  frontend types, and controls on the existing Risk page. The polled list and the
  explicitly reviewed mutation version are separate; changing an instrument or a
  failed write invalidates review. No new static page or fabricated state was added.
- Focused actual-service/browser tests: **6 passed / 0 failed / 0 skipped**,
  `backend/logs/instrument-blocks-focused.txt`; pipeline/risk-safety selection:
  **34 passed / 0 failed / 0 skipped**. Frontend **32 passed**, production build
  successful. Complete final-tree backend regression: **1418 passed / 0 failed /
  3 PostgreSQL-only skips**, in twelve disjoint
  `backend/logs/instrument-blocks-final-full-0.txt` through
  `backend/logs/instrument-blocks-final-full-11.txt`;
  every final batch exited 0 and real browser scenarios were enabled. Focused
  counts overlap and are not added to the full-suite total.
- The first regression run caught an ordering defect introduced by the new catalog
  guard: a future-dated advisory receipt encountered catalog chronology validation
  first, incorrectly causing a global risk-error latch. Receipt validation now
  precedes the new instrument guard; invalid advisory receipts retain their named
  containment rejection, while valid receipts still face every entry veto. Existing
  proposal-agent plus instrument/browser regression: **24 passed / 0 failed**,
  `backend/logs/instrument-blocks-ordering.txt`. The complete final tree was rerun;
  earlier failed `instrument-blocks-full-*` logs are not passing evidence.
- Tests prove authenticated controls, durable restart, all-origin manual scope,
  named shared-pipeline rejection, late block after preflight with no broker call,
  duplicate-call safety, current catalog changes, preserved exits and real React
  block/release actions. No live broker, exchange-ban feed or remote delivery claim.
- RISK-014 remains partial; counts stay **226 tested / 13 implemented-unverified /
  125 partial / 169 not started**. See `docs/INSTRUMENT_BLOCKS.md`. PAPER remains
  default; Groww LIVE/deployed PostgreSQL remain unverified; M1/M2 remain incomplete.
- Exact next task: close the remaining server-resolved event-blackout gap in
  RISK-014 using existing point-in-time corporate/event calendar sources. Enforce
  owner/calendar restrictions at shared decisions and final dispatch, not just
  caller flags or reference-strategy-only checks; preserve exits and auditable
  rejection evidence. EXCH-008 maintained F&O ban sourcing remains a distinct gap.

### Server-held PAPER event calendar controls (2026-10-02)

- Owner-published existing event/corporate calendar evidence now reaches the shared
  quantitative/advisory decision path, approval transaction, execution preflight
  and final entry dispatch. Caller flags and optional strategy declarations cannot
  bypass configured blackouts. Named vetoes are `EVENT_BLACKOUT` and
  `EVENT_CALENDAR_UNAVAILABLE`; exits remain available.
- Corporate imports and control audit publication commit atomically. Controls pin
  exact instrument/origin, optional strategy scope, explicit windows and freshness,
  source/knowledge/coverage dates and reviewed predecessor. Restart reloads audited
  history. Current clock regression fails closed instead of falling back to an
  older calendar; explicit historical reads remain point-in-time correct.
- Approved proposal and order snapshots archive calendar evidence. Existing Risk
  view gains typed authenticated list/read/publication APIs at
  `/api/v1/risk/event-controls`; review versions stay separate from polled state.
  Failed writes invalidate review. No new page or fabricated evidence was added.
- Final-tree backend selection: **760 passed / 0 failed / 0 skipped** in
  `backend/logs/event-controls-regression.txt` (exit 0). Includes all unit/e2e/safety
  directories, actual PAPER execution/reference-worker/proposal-agent/risk-safety
  integrations, corporate calendar tests and real Edge control verification.
  Frontend **33 passed**, production build successful; changed Python files pass
  Ruff. This is a broad selection, not a fresh complete backend run. The latest
  complete suite remains the prior checkpoint's **1418 passed / 3 PostgreSQL-only
  skips**; do not present that count as verification of this changed tree.
- Tests cover late publication after preflight, denied entries, explicit disable,
  stale/uncovered/future evidence, corporate audit rollback, strategy scope,
  recovered position exits and clock rollback. Initial browser runs caught
  accessible-label mismatches on the select/textarea; explicit accessible names
  fix them and the final regression includes the passing real-browser scenario.
  The existing fallback fixture Decimal-serialization warning remains unchanged.
- RISK-014 and EQ-007 remain partial. Counts stay **226 tested / 13
  implemented-unverified / 125 partial / 169 not started**. No external calendar
  feed, exchange ban list, cross-process PostgreSQL, Groww LIVE or M1/M2 claim.
  See `docs/EVENT_CONTROLS.md`. Owner README/migration/chat files remain untouched.
- Exact next task: integrate the same server-held event evidence into upstream
  advisory context (EQ-007) without changing receipt-bound inputs after issuance;
  verify the actual worker/advisory boundary and run a complete backend regression
  at that checkpoint. Maintained exchange F&O ban sourcing (EXCH-008), derivative
  scope and external verification remain separate gaps, not inferred successes.

### Advisory calendar context and complete regression (2026-10-02)

- The shared advisory service now discards caller calendar assertions and resolves
  configured instrument/origin/strategy evidence from storage. The typed snapshot
  is included in escaped untrusted input and the sealed request audit; it grants
  no execution authority. Unconfigured remains null, not invented clear evidence.
- Request context remains immutable. Receipt validation checks calendar identity
  and knowledge timestamps, while current deterministic gates independently veto
  newly active events. A real receipt survives database restart and is rejected
  when the scheduled blackout begins, without changing the original archived
  request or creating a broker order. Historical/replay requests still bypass the
  remote model and discard caller calendar assertions.
- Extended the actual worker/Claude HTTP-boundary tests across review/proposal,
  continue/abstain and timeout/fallback paths with owner-published calendars. New
  tests cover active event context overriding a caller's invented clear status,
  clock-regression failure before paid HTTP, and restart/current-blackout receipt
  rejection. This verifies fixture HTTP routing, not external Claude connectivity.
- Focused final selection: **41 passed / 0 failed / 0 skipped** in
  `backend/logs/calendar-advisory-final-focused.txt`. Complete final-tree backend:
  **1431 passed / 0 failed / 3 PostgreSQL-only skips**, thirteen disjoint batches
  `backend/logs/calendar-advisory-full-0.txt` through `-12.txt`, all exit 0;
  real Edge browser cases enabled. Focused counts overlap the full run. Existing
  Decimal-serialization/deprecation warnings are not new failures. Changed-file
  Ruff passes. Frontend source is unchanged from the preceding **33 passing tests
  and successful production build**; its real browser integrations ran again in
  this complete backend regression.
- EQ-007 remains partial: configured owner publications reach advisory context,
  but automatic convergence/refresh across all corporate/reference calendar inputs
  is not established. RISK-014 still lacks maintained exchange ban sourcing.
  Counts remain **226 tested / 13 implemented-unverified / 125 partial / 169 not
  started**. PAPER default, Groww LIVE unverified, and no M1/M2 completion claim.
- Exact next task: implement sourced, dated F&O ban-list admission and server-held
  entry enforcement for EXCH-008/RISK-014, preserving exits and provenance. First
  inspect existing catalog/contract preflight and PAPER F&O limitations; do not
  treat current catalog flags or owner-entered caller booleans as a maintained
  exchange list, and do not claim live feed verification without actual evidence.

### Dated NSE F&O ban report admission (2026-10-02)

- Inspected the actual boundary: PAPER execution still rejects non-CASH entries;
  fee schedules and contract production remain CASH/MIS-specific. Those gates are
  preserved. This checkpoint does not claim a functioning F&O execution lifecycle.
- Added strict source-format report parsing, embedded trade dates, retained raw
  document/hash and authenticated audited admission. A public NSE CSV read confirmed
  the parser format; that document was not ingested as application state or used
  as synthetic test evidence. Scheduled production acquisition remains unverified.
- Current-session selection is IST-date/origin specific. Tomorrow publications do
  not supersede today's applicable report; prior-day data never implies clear
  coverage. Conflicting same-knowledge and older same-date revisions are refused.
  Historical reads exclude later admissions. Missing, corrupt or future-clock
  evidence fails closed. Canonical underlying matching covers strikes/expiries
  without deriving symbols from contract names; unsupported exchange/mapping is
  explicitly unavailable rather than clear.
- Shared instrument entry guards now enforce `FNO_BAN_LISTED`,
  `FNO_BAN_REPORT_UNAVAILABLE` and `FNO_BAN_SCOPE_UNAVAILABLE` independently of
  caller booleans. Existing decision/preflight/final entry paths use that guard.
  CASH entries and protective exits are outside this F&O-only veto. Actual F&O
  order/fill/exit proof still awaits removal of the CASH-only limit through a
  separately verified lifecycle, not a bypass in these tests.
- Existing Risk page now admits reviewed documents via typed
  `GET/PUT /api/v1/risk/fno-bans`; review/version conflicts invalidate the form.
  Browser tests exercise real authenticated APIs and durable report state.
- Final broad regression: **751 passed / 0 failed / 0 skipped** in
  `backend/logs/fno-bans-regression.txt`, including all unit/e2e/safety directories,
  ban and manual/event controls, proposal receipts and PAPER execution integrations;
  real browser scenarios enabled. Frontend **34 passed**, production build
  successful. Ruff and diff checks pass. This selection is not a fresh complete
  backend run; latest complete suite is preceding `d1a2ec3`'s **1431 passed / 3
  PostgreSQL-only skips**. Existing fallback Decimal warning remains unchanged.
- EXCH-008 moves only to partial; RISK-014 remains partial. Counts: **226 tested /
  13 implemented-unverified / 126 partial / 168 not started**. See
  `docs/FNO_BANS.md`. No Groww LIVE, scheduled exchange-feed, BSE source, PostgreSQL
  cross-process or M1/M2 verification claim. Owner files remain untouched.
- Exact next task: inspect and implement the missing F&O PAPER lifecycle as a
  coherent vertical slice, beginning with a defined-risk single long option and
  explicit sourced fee/contract inputs, lot/tick/expiry/ban enforcement, actual
  OMS fills, protection, exit and net accounting. Keep the current CASH-only gate
  until the new path is genuinely integrated and tested; keep futures/naked shorts
  blocked unless independently supported. Do not stop at adding another isolated
  option calculation or suggest that this ban-control checkpoint enables trading.

### Defined-loss sizing prerequisite for the option lifecycle (2026-10-02)

- Tracing the actual option path found a concrete mismatch: `RiskProposal` uses
  the greater of stop distance and defined maximum loss, but the shared sizer
  ignored the defined bound. This caused inappropriate proposed quantities which
  the independent risk engine then vetoed; the fix does not weaken that veto.
- Shared sizing now receives the same source bound and uses
  `max(stop_distance, defined_max_loss_per_unit) + costs`, retaining lot rounding
  and every margin/exposure/daily cap. Bound-aware calculations record formula
  `1.1.0`; legacy stop-only calculations retain `1.0.0`. Replay checks the actual
  stored formula version and survives process/database restart.
- Hand-computed cases prove full-loss sizing, charges, whole-lot refusal and that
  a small declared bound cannot reduce stop risk. Actual shared pipeline evidence
  proves a 500 risk budget, 100 per-unit bound and quantity 5 agree with independent
  risk and persisted replay. The fixture supplies a target with sufficient reward
  against that full bound; the existing reward/risk gate remains unchanged.
- Final broad backend selection: **767 passed / 0 failed / 0 skipped**, including
  all unit/e2e/safety directories plus sizing, shared pipeline, risk, risk-safety,
  PAPER execution and proposal-agent integrations; log
  `backend/logs/defined-loss-sizing-regression.txt`, exit 0. Focused selection
  **37 passed** overlaps it. Changed-file Ruff passes. Earlier test-edit placement
  and insufficient fixture reward failures were fixed before final validation.
  No fresh complete-suite or frontend rerun is claimed: latest complete backend
  remains `d1a2ec3`'s 1431/3 skips; unchanged frontend was 34 passing/build successful
  at `99da641`. Existing fallback Decimal warning remains unchanged.
- This is a necessary correction in the option vertical slice, not completion
  of that slice. F&O OMS remains blocked. SIZE-001/SIZE-008 traceability is updated;
  no requirement promotion. Counts remain **226 tested / 13 implemented-unverified /
  126 partial / 168 not started**. PAPER and all existing safety gates remain.
- Exact next work in the same vertical slice: make PAPER account/constraints
  distinguish a single long option from generic F&O margin estimates, persist that
  identity through fills/restart and prohibit naked shorts. Then connect explicit
  option fee/contract/Greek evidence to worker and OMS, preserving lot/tick/expiry/
  liquidity/ban checks, before lifting the CASH-only guard. Current generic F&O
  margin uses only product/segment; it is not evidence of safe option accounting.

### Typed option PAPER broker accounting (2026-10-02)

- Persisted option identity now selects full-premium reservation, rounded upward
  to paise, instead of generic F&O margin estimates. FIFO partial entry/exit and
  restart retain that identity. Naked sells/reversals, missing contract metadata
  and expired contracts fail closed. Restore checks order/fill/position identity
  and reservation totals before accepting persisted option state.
- Final broad regression: **783 passed / 0 failed / 0 skipped**, recorded in
  `backend/logs/option-account-final-regression.txt`, exit 0. It includes all
  unit/e2e/safety tests plus PAPER account, constraints, broker, FIFO, execution,
  costs, reference worker, stops and liquidity integrations. Existing fallback
  Decimal warning remains. Changed constraints/tests pass Ruff; existing legacy
  provider/account lint diagnostics have no additions. Diff check passes.
- This is broker-level gross accounting, not an enabled F&O OMS lifecycle.
  Production CASH-only guard remains intact. No new frontend/API changes or
  fresh full-suite claim: complete backend baseline remains 1431 passed/3 skips
  at `d1a2ec3`; frontend remains 34 passing/build successful at `99da641`.
- PAPER-003/PORT-002 traceability updated without promotion. Counts unchanged:
  **226 tested / 13 implemented-unverified / 126 partial / 168 not started**.
  See `docs/PAPER_OPTIONS.md`. Owner changes remain untouched; nothing pushed.
- Exact next task: bind explicit option tariff and contract/Greek evidence to
  the existing worker/shared pipeline and net OMS lifecycle, preserving independent
  preflight and final-dispatch gates. Only lift the CASH-only gate once actual
  option protection/exit/net journal/restart scenarios prove the complete path.

### Explicit option-premium fee integration (2026-10-02)

- Extended the existing audited tariff schema/API with explicit FNO/MIS
  `OPTION_PREMIUM` basis. CASH remains backward-compatible; no tariff rates are
  invented. Cost selection requires catalog option identity. Broker fills also
  reject wrong exchange/segment/product or missing captured option identity.
  The shared contract gate independently refuses option tariffs for futures.
- Real broker partial fills and restart consume the tariff, preserving cumulative
  brokerage caps and charges. A synthetic buy 100 at 100/sell 100 at 110 produces
  gross 1000, estimated charges 28.62, net 971.38. Prior-knowledge, unknown symbol,
  futures exclusion and malicious/mistaken cost-source scope tests pass. These
  are premium trading charges, not exercise/settlement or live billing proof.
- Broad regression **766 passed / 0 failed / 0 skipped**, log
  `backend/logs/option-fees-regression.txt`, exit 0. Final focused selection after
  broker and shared-pipeline rejection tests: **30 passed / 0 failed / 0 skipped** (overlaps broad
  selection; counts are not additive). Initial fixture keyword error was fixed.
  Frontend generated types refreshed, **34 tests passed**, production build passed.
  Existing fallback Decimal warning persists. Latest complete backend suite is
  still the earlier 1431 passed/3 PostgreSQL skips, not a fresh full-suite claim.
- PNL-003 remains partial. Counts unchanged: **226 tested / 13 unverified /
  126 partial / 168 not started**. No new pages. F&O OMS remains CASH-blocked;
  option worker/Greek/protection/net journal integration is not complete.
- Exact next task: connect a server-held option contract/chain/Greek evidence
  source through reference runtime and shared risk, then prove the actual
  single-long-option OMS lifecycle before lifting that gate. Retain existing
  reward/risk, ban, expiry, liquidity, lot/tick and PAPER-only restrictions.

### Provider option evidence reaches the reference worker (2026-10-02)

- Added `OptionEvidenceSource` through the existing market-data provider interface
  and reference runtime. Exact NSE catalog identity, expiry, strike, side, symbol
  and origin must match; complete timestamped provider Greeks must pass configured
  freshness and value checks. No invented volatility, computed fallback or feed
  verification is implied. Unsupported/incomplete evidence stands down.
- Accepted observations persist via the existing chain snapshot store and a
  reconstructable audit record containing contract, chain and Greek values.
  The resulting evidence ID populates the worker market context. Actual worker
  tests reach this producer and verify missing-Greek stand-down and zero orders;
  they do not bypass F&O ban or the CASH-only execution gate.
- Final broad backend regression **746 passed / 0 failed / 0 skipped**, log
  `backend/logs/option-evidence-regression.txt`, exit 0. It includes all unit,
  e2e and safety tests plus option evidence, reference worker, chain snapshots,
  shared pipeline and PAPER execution. Focused 11 passing tests overlap it.
  Ruff and diff checks pass; existing fallback Decimal warning remains.
- No frontend changes; preceding `df693e3` has 34 passing frontend tests and a
  successful build. Complete backend baseline remains 1431 passed/3 skips until
  a new complete run finishes. No external market/Groww verification claim.
- GRK-008/OC-007 traceability updated, no promotions. Counts unchanged:
  **226 tested / 13 implemented-unverified / 126 partial / 168 not started**.
- Exact next task: verify the complete suite, then bind this audited option
  evidence and explicit premium tariffs to contract-cost production and independently
  revalidate source linkage in shared decision/preflight. Integrate minimum DTE
  and a risk-compatible reference option hypothesis before proving OMS entry,
  protection, exit and net journal. Keep the CASH-only gate until that entire
  path passes; do not claim this ingestion checkpoint enables F&O trading.

### Complete-suite verification and option source authorization (2026-10-02)

- Complete backend suite at `9c6264e`: **1472 passed / 0 failed / 3 skipped**
  across thirteen disjoint batches in `backend/logs/option-worker-full-0.txt`
  through `-12.txt`; every batch exits 0 and the parent reports
  `FULL_SUITE_COMPLETE`. Real browser scenarios enabled. Skips require
  `ATS_TEST_POSTGRES_URL`. The original process was retained through long batches.
- Subsequently added independent persisted-source authorization to shared decision
  and execution entry-source guards. Option Greek evidence must resolve to its
  original PAPER producer audit chain, instrument, origin, original knowledge
  timestamps and unchanged catalog contract. Stale, missing, forged, retimestamped
  or catalog-drifted evidence fails closed. It cannot be refreshed by changing a
  caller timestamp. Existing CASH behavior and the F&O OMS block remain intact.
- Rehydration now normalizes chain strikes to immutable tuples, matching the
  existing snapshot store. An initial tuple/list mismatch and a test fixture clock
  mismatch were caught and corrected before final regression; no safety gate was
  relaxed. Positive/forged shared decision-gate tests and restart/source-boundary
  tests exercise the actual stored evidence. Positive option OMS proof is pending.
- Final regression of source-gate changes: **748 passed / 0 failed / 0 skipped**,
  `backend/logs/option-source-gates-regression.txt`, exit 0. Includes all unit/e2e/
  safety plus option evidence, shared pipeline, PAPER execution and worker tests.
  Ruff and diff checks pass. Existing fallback Decimal warning remains. The new
  complete-suite baseline above precedes these source-gate changes; no claim that
  the complete suite was rerun afterward. Frontend unchanged from 34 passing/build
  successful at `df693e3`.
- No requirement promotions. Counts: **226 tested / 13 implemented-unverified /
  126 partial / 168 not started**. PAPER default, no external verification, no push.
- Exact next task: bind the audited option source to a typed owner-published
  long-option contract policy with minimum DTE, explicit premium tariff and
  conservative full-premium sizing, then integrate a declared reference option
  hypothesis and actual OMS lifecycle. Check the remaining risk-bound issue before
  enabling: a declared maximum loss must not fall below an actual long-option
  entry premium merely because the quote moved since source capture. Keep naked
  shorts/futures and the existing CASH-only gate blocked until independently proven.

### Long-option policy, premium floor and independent veto (2026-10-02)

- Existing reference-input publication accepts an explicit `LONG_OPTION` policy
  with owner-supplied minimum calendar DTE, evidence origin, validity window and
  cost reserve. Worker contract production uses the existing expiry selector,
  revalidates audited Greeks, resolves the explicit premium tariff and reserves
  full premium. Audit links the original policy, market observation and option
  evidence. Missing Greeks or insufficient DTE produces no contract/order.
- Actual worker integration tests derive premium/margin 100 from a synthetic
  quote and tariff, and reject the same seven-day fixture when policy requires
  eight days. The existing CASH worker path remains covered. No option order is
  enabled by this change; the CASH-only OMS gate remains intact.
- Shared sizing and risk proposal construction floor option loss, margin and
  exposure at the actual conservative tick-rounded entry. Hand-computed proof:
  a 100.01 entry rounds to 100.05; even with a prior declared bound of 99, a 500
  risk budget buys four units, not five. Pure risk independently rejects understated
  option premium bounds and naked short options; no caller can enable the latter.
- Option risk formula `1.1.0` distinguishes the new vetoes. CASH decisions retain
  their original rule output/version. Audit replay explicitly reproduces historical
  option `1.0.0` decisions without making that legacy evaluator an execution option;
  current evaluation still rejects them. Version-tampering tests fail safely.
- Final broad regression: **762 passed / 0 failed / 0 skipped**, log
  `backend/logs/long-option-policy-regression.txt`, exit 0. Includes all unit/e2e/
  safety, contract/option production, pipeline, risk replay, execution and worker
  tests. Rule-registry expectations were updated for the new rule; final checks pass.
  Ruff/diff checks pass. Frontend **34 passed**, final generated API types and
  production build successful. Existing fallback Decimal warning unchanged.
- Complete backend baseline remains **1472 passed / 3 PostgreSQL-only skips** at
  `9c6264e`, not a claim of a new full run. OPT-006 moves only to partial; explicit
  short-option enablement/capped structures are intentionally unavailable. Counts:
  **226 tested / 13 implemented-unverified / 127 partial / 167 not started**.
- Exact next task: integrate a declared reference long-option hypothesis with
  reward/risk appropriate to full-premium loss, then seal/revalidate its actual
  published contract policy at preflight and final dispatch and prove the real
  OMS entry/protection/exit/net journal/restart lifecycle. Do not remove the
  CASH-only gate until that positive path and unsafe-order cases are tested.

### Reference option hypothesis reaches risk approval (2026-10-02)

- The existing breakout implementation now has a distinct `long-option-breakout`
  specification/ID rather than silently repurposing the equity registration.
  It retains closed-candle/SMA/ATR/regime rules and declared exits, with a fixed
  four-premium target hypothesis and full-premium sizing. This is not a prediction,
  validated strategy or claimed return. See its explicit SPEC.md for missing
  backtest/OOS/walk-forward/PAPER performance evidence.
- Runtime discovery, instrument-class checks, exit reconstruction and coverage
  observation now recognize both reference IDs. Authenticated registration accepts
  an explicit LONG_OPTION kind and still registers disabled. The existing Strategy
  page exposes the choice and truthfully warns that F&O OMS remains blocked.
- The actual worker test registers/enables the option hypothesis through APIs,
  admits a synthetic clear ban report, ingests provider candles/chain/Greeks,
  derives source-backed costs and produces an approved four-unit decision through
  the shared validation/sizing/risk path. The OMS then records EXECUTION_BLOCKED
  and creates zero orders, as required by the still-active CASH-only guard. The
  original test expectation of a final RISK_APPROVED status was corrected to
  verify both the approved risk decision and the subsequent execution block.
- Final regression **737 passed / 0 failed / 0 skipped** in
  `backend/logs/option-strategy-regression.txt`, exit 0. It includes all unit/e2e/
  safety selections, option/contract production, worker and the real Edge-backed
  costed CASH trade/dashboard scenario. The original long-running test process
  completed; it was not restarted on an observation timeout. Frontend **34 passed**,
  generated API types and production build successful. Ruff/diff checks pass;
  existing fallback Decimal warning remains. Latest full-suite baseline remains
  **1472 passed / 3 PostgreSQL-only skips** at `9c6264e`.
- STRAT-005 traceability updated; no completion promotion. Counts unchanged:
  **226 tested / 13 implemented-unverified / 127 partial / 167 not started**.
- Exact next task: seal and independently revalidate the owner-published option
  contract policy at preflight/final dispatch, populate full option metadata in
  OMS broker constraints, and reserve full-premium risk for open option positions.
  Then replace the blanket CASH gate only with narrowly validated long-option
  authorization and prove entry/fill/protection/stop-exit/net journal/restart and
  blocked-order cases through actual OMS services. Do not label this checkpoint
  an option trading lifecycle; no option execution or external Groww proof exists.

### Verified checkpoint: single-long-option PAPER OMS (2026-10-02)

- Working tree replaces the blanket CASH restriction with narrow NSE/MIS LONG
  option authorization for the declared reference hypothesis. The original audited
  contract observation and still-current owner policy must match at preflight and
  final dispatch. Futures/shorts/unsupported structures still fail closed.
- OMS broker constraints now carry real catalog option identity. Open-position
  reserved risk includes full premium/current value and planned fee risk backed
  by the original risk approval, not merely the stop distance.
- Focused tests pass through actual provider ingestion, reference strategy, shared
  decision/sizing/risk, order, fill, protection, open/pending restart, stop exit,
  FIFO, costs, journal, audit, notification requests and real Edge/React state.
  The synthetic four-unit 100-to-96 stop fixture records gross -16, charges 11.93,
  net -27.93. No performance or external notification delivery is claimed.
- Negative tests pass for stale input, disabled strategy, error latch, missing
  Greeks, minimum DTE, forged/replaced contract policy, policy revocation after
  preflight, broker failure without duplicate submission, missing protection and
  authenticated emergency close. An incorrect assumption that Proposal contains
  an exchange field was caught and fixed before the positive execution test.
- Frontend **34 tests passed**, final generated types/build successful; existing
  Orders/Positions now expose segment instead of disguising derivatives as CASH.
- Complete backend regression: **1495 passed / 0 failed / 3 skipped**, thirteen
  disjoint batches in `backend/logs/option-oms-full-0.txt` through `-12.txt`.
  All batches exited zero and the parent reported FULL_SUITE_COMPLETE. Actual
  Edge browser scenarios were enabled. The three skips require PostgreSQL;
  existing Decimal/SQLite warnings remain. No slow batch was restarted.
- GRK-008, STRAT-005, PORT-002 and PNL-003 traceability now reflects the integrated
  option path, without promoting broader acceptance criteria. Counts remain
  **226 tested / 13 implemented-unverified / 127 partial / 167 not started**.
- PAPER remains default. No Groww/live-feed, external notification delivery,
  strategy profitability, time-based PAPER validation or M1/M2 claim.
- Exact next task: extend the isolated historical runner to the same narrowly
  authorized long-option strategy, requiring recorded chain/Greek observations,
  point-in-time instrument metadata, explicit premium tariffs and dated ban
  evidence. Missing chain history must fail explicitly, not approximate prices
  or Greeks. Prove the actual costed lifecycle and reproducibility before marking
  BT-012 complete; futures and broader derivatives remain unsupported.

### Verified checkpoint: recorded long-option historical runner (2026-10-02)

- The existing isolated historical bootstrap now accepts complete NSE option
  metadata and selects the actual long-option reference strategy. It requires
  explicit source-dated premium tariffs, the original owner option policy and a
  REPLAY-origin ban report known by the start of the same Indian session. No
  futures, multi-session options, settlement or generated historical observations.
- Recorded chain history must match underlying/expiry and be available at start;
  missing, future-only or wrong-expiry chains fail before any run/account writes.
  The shared worker still independently validates current contract/Greek evidence,
  source lineage, sizing, risk and preflight. Banned or missing-Greek inputs cannot
  produce orders. The snapshot replay provider is reused, not replaced.
- Actual strategy/OMS/protection/FIFO/cost/journal replay: synthetic entry four
  units at 100, stop exit at 96, gross -16, charges 11.93, net -27.93. A window
  ending before the recorded exit remains INCOMPLETE with four units open. Two
  isolated child processes produce identical economic fingerprints. This is
  deterministic software evidence, not real-market performance validation.
- Review found that the walk-forward controller currently fixes its report
  identity to the equity hypothesis. Option experiments now fail explicitly at
  input freezing rather than receive misleading equity attribution. Correct
  option walk-forward/OOS integration remains pending.
- Regression **785 passed / 0 failed / 0 skipped** in
  `backend/logs/option-history-regression.txt`, including all unit/e2e/safety,
  historical/backtest/option tests and actual option Edge browser execution.
  Following the walk-forward scope guard, final affected regression **29 passed /
  0 failed / 0 skipped** in `backend/logs/option-history-walkforward-final.txt`.
  These overlap and must not be summed. An initial test-only FileExistsError was
  fixed by writing a separate fixture recording, preserving immutable production
  recording behavior. Ruff and diff checks pass.
- Latest full-suite baseline is **1495 passed / 0 failed / 3 PostgreSQL-only
  skipped** at `70067ac`; not a new full-suite claim for this historical extension.
  Frontend source/schema are unchanged; previous **34 passed / build successful**
  remains the frontend baseline. Option historical publication/dashboard/export
  still needs its own verification; PAPER option browser evidence is separate.
- BT-012 moves only to partial. Counts: **226 tested / 13 implemented-unverified /
  128 partial / 166 not started**. No external Groww, live feed, tariff billing,
  notification delivery, profitability, time-based PAPER or M1/M2 claim.
- Exact next task: publish the recorded option result through the existing sealed
  historical catalog and verify authenticated API plus actual React historical
  dashboard/export attribution and net costs. Then integrate correct option
  strategy identity into chronological walk-forward/OOS; do not simply remove
  its explicit unsupported guard or label an option run as an equity strategy.

### Verified checkpoint: option historical API/dashboard/export (2026-10-02)

- The existing Backtests page now displays the actual strategy ID and version
  from the typed backend response, rather than leaving option/equity attribution
  implicit. No new page, fake response or alternate publication service.
- The actual Edge scenario now covers both equity and option owner plans:
  authenticated login/launch, real historical child process, sealed cross-database
  publication, charts, declared universe, reload and downloaded simulated report.
  The option export retains `long-option-breakout` version 1, AUDIT_BOUND integrity,
  gross -16, charges 11.93 and net -27.93 from the actual replayed journal.
  Returning through the authenticated Dashboard verifies no historical orders or
  positions were imported into the active PAPER account.
- Frontend **34 passed**, TypeScript/production build successful. Final backend
  regression **26 passed / 0 failed / 0 skipped** in
  `backend/logs/option-report-regression-final.txt`: all PAPER browser scenarios,
  historical jobs, authenticated read/export API and publication tests. Ruff/diff
  checks pass. An initial new account-isolation probe received the correct 401
  because a raw browser HTTP request omitted the app's authentication header;
  the test now uses actual authenticated UI navigation, without weakening auth.
- Full-suite baseline remains **1495 passed / 3 PostgreSQL-only skips** at
  `70067ac`; subsequent historical regression evidence is recorded separately.
  BT-012 stays partial; counts unchanged: **226 tested / 13 implemented-unverified /
  128 partial / 166 not started**. No live feed/Groww, performance, time-based
  PAPER, external notification or M1/M2 verification claim.
- Exact next task: bind the walk-forward parent to the actual declared strategy
  identity rather than hardcoded equity identity, reject mixed-family experiments,
  and prove real option training-only selection followed by isolated chronological
  OOS trades and correctly attributed sealed reports. Only remove the current
  option unsupported guard when that integrated path passes its negative cases.

### Verified checkpoint: option walk-forward integration (2026-10-02)

- Working tree derives the historical and walk-forward strategy identity from
  the validated selected instrument, instead of assigning the equity identity
  to every parent report. Input freezing rejects mixed strategy families and
  requires matching initial option-chain history for every train/OOS child.
- Real option child-process tests pass for training-only risk-fraction selection,
  followed by either a losing or favorable OOS recording. Altering OOS outcomes
  does not alter the selected candidate. Parent, child and bound OOS specification
  retain `long-option-breakout`; training trades remain excluded from OOS totals.
  Duplicate/restart requests do not launch another experiment. Missing OOS chain
  and mixed-family plans fail before child execution.
- Focused **18 passed / 0 failed / 0 skipped** in
  `backend/logs/option-wf-focused-final.txt`. One negative test initially read the
  recording envelope as a bare bundle; fixed to use the actual recording reader
  and write a separate immutable fixture. Ruff/diff checks pass.
- Full backend verification **1514 passed / 0 failed / 3 skipped** in thirteen
  disjoint batches `backend/logs/option-wf-full-0.txt` through `-12.txt`, actual
  browser scenarios enabled. All batches exited zero; parent FULL_SUITE_COMPLETE.
  PostgreSQL-only schema checks remain skipped. Existing Decimal/SQLite/event-loop
  warnings remain; no slow batch was restarted and tested source was frozen.
- Frontend code/schema unchanged in this checkpoint; previous **34 tests passed /
  production build successful** remains the frontend baseline. Complete browser
  regression includes standalone option history and equity OOS, not a claim of
  option-specific OOS browser verification.
- BT-012 traceability updated without promotion: futures and multi-session open
  position carry remain unsupported. Counts remain **226 tested / 13 implemented-
  unverified / 128 partial / 166 not started**. No live/external verification,
  profitability, time-based PAPER or M1/M2 claim.
- Exact next task: implement BT-014 across the real historical report/API/UI:
  bind the existing configured `min_backtest_days` to captured run settings,
  expose exact window duration and an explicit below-minimum/coverage-unverified
  warning without equating elapsed calendar span with continuous observations or
  statistical confidence. Verify option OOS report inspection alongside those
  report changes; preserve source-bound sealed historical reports and no-lookahead.

### Verified checkpoint: captured historical-window disclosure (2026-10-02)

- BT-014 now connects the existing positive `min_backtest_days` setting to the
  actual isolated run's captured configuration, report assumptions and audit
  digest. Changing the setting after preparation is rejected. The runner persists
  exact UTC-instant elapsed duration, requested endpoints, bounded publication
  counts and first/last available timestamps for the selected instrument.
- `BELOW_MINIMUM` warns about short windows. `SPAN_MEETS_MINIMUM` never means
  complete exchange sessions, independent samples, statistical confidence or
  profitability. Continuous coverage/confidence stay explicitly unverified,
  including sparse long windows and missing observations. DST uses actual
  instants, not wall-clock duration. No prices or observations are fabricated.
- OOS parent reports retain only selected OOS children's captured windows,
  not training counts or a fabricated continuous portfolio span. Typed API,
  download export and the existing Backtests page display the stored disclosure.
  Legacy detail/export/UI show unavailable policy metadata and do not rewrite
  sealed reports using current settings. The three-month Groww history limit is
  identified as the existing project-design limitation, not verified API coverage.
- Real option OOS Edge verification now covers authenticated launch through
  actual child execution/publication, `long-option-breakout` attribution, two-unit
  synthetic OOS trade, positive charges/net loss, selected OOS-only duration,
  BELOW_MINIMUM warning, AUDIT_BOUND export and reload. This is software evidence,
  not real-market performance, approval or time-based PAPER validation.
- Broad regression **819 passed / 0 failed / 0 skipped** in
  `backend/logs/history-adequacy-regression.txt`: all unit/e2e/safety, historical,
  option and walk-forward integration plus all actual PAPER browser scenarios.
  The same authoritative process completed after 1187 seconds; quiet buffered
  output was checked through its live process/child activity, never restarted.
  Final legacy/API/adequacy regression **8 passed / 0 failed / 0 skipped** in
  `backend/logs/history-adequacy-legacy-final.txt` overlaps that broad selection.
- Frontend **34 passed**, generated types and production build successful.
  New/modified analysis, report, API and test modules pass Ruff. The two-line
  config change preserves its existing 27 lint diagnostics, confirmed against
  HEAD in `backend/logs/config-lint-baseline.txt`; no unrelated style rewrite.
  Latest complete-suite baseline remains **1514 passed / 3 PostgreSQL-only skips**
  at `e185ef7`, not a new full-suite claim for this report extension.
- BT-014 promoted only after integration tests and actual browsers passed.
  Counts: **227 tested / 13 implemented-unverified / 128 partial / 165 not started**.
  PAPER default preserved; Groww LIVE, external sources/billing/notifications,
  profitability and M1/M2 remain unverified.
- Exact next task: close BT-001/BT-002 acceptance gaps with full-session
  PAPER/backtest parity and deliberately attempted look-ahead through actual
  provider/strategy boundaries. Inspect existing replay/session tests first;
  preserve the one shared worker, no duplicate simulator or fictitious fills.

### Verified checkpoint: replay parity and adversarial strategy boundary (2026-10-02)

- A real historical-worker reproduction exposed an uncaught next-bar `IndexError`:
  the provider correctly withheld future candles, but the worker globally failed
  before recording a normal strategy rejection. Reproduction evidence is in
  `backend/logs/lookahead-reproduction.txt`; no unsafe order was submitted.
- Working tree converts strategy indexing violations into an explicit no-signal
  `STRATEGY_DATA_BOUNDARY_VIOLATION`, retained by the existing decision/audit path.
  Historical reports count invalid strategy evaluations and become INCOMPLETE,
  rather than treating broken strategy logic as a successful zero-trade result.
  Next-bar, undeclared-input and future-signal cases all pass through actual
  application boundaries, with audited rejection, no order and INCOMPLETE research.
- A full sampled-session parity test passed through two real production
  entry points: historical runner and directly cycled PAPER worker with the same
  recorded provider. It compares signals/proposals, candidates, sizing, risk,
  orders/fills, positions, costed journal and session/evaluation audit semantics.
  No substitute fill model. FIFO comparison normalizes only run-local fill IDs
  while retaining their linkage and all accounting values. Both paths end with
  fixture cash 100856.56, no reserved margin and no open positions.
- Parity/adversarial acceptance: **4 passed / 0 failed / 0 skipped** in
  `backend/logs/replay-boundaries-final.txt`. Broad strategy/pipeline/replay/
  walk-forward regression: **761 passed / 0 failed / 0 skipped** in
  `backend/logs/replay-safety-regression.txt`. Ruff passes touched Python files.
  Frontend unchanged: prior **34 passed**, types/build successful. Latest full
  suite baseline remains **1514 passed / 3 PostgreSQL-only skips** at `e185ef7`.
- BT-001/002 promoted against these integration acceptance tests. Counts:
  **229 tested / 13 implemented-unverified / 126 partial / 165 not started**.
  No LIVE, external source or M1/M2 claim. This tests declared application
  boundaries, not a sandbox against arbitrary hostile Python.
- Exact next task: reproduce and fix the reference strategy's advancing-clock
  decision timestamp mismatch. Engine requires signal timestamp to equal captured
  `as_of`, while reference entry reads the clock again after asynchronous work;
  frozen fixture clocks may conceal real PAPER rejection. Preserve strict future
  input/signal checks and verify through the existing shared decision path.

### Verified checkpoint: advancing-clock PAPER reference lifecycle (2026-10-02)

- Reproduced valid signal rejection with a test clock advancing one microsecond
  per read: actual worker ingestion reached strategy evaluation but produced
  INVALID_OR_UNAVAILABLE_STRATEGY_INPUT_OUTPUT. Reproduction log:
  `backend/logs/advancing-clock-reproduction.txt`. No unsafe order was sent.
- Root cause: entry read the clock again after the engine captured `as_of`.
  Context now carries immutable aware decision-time metadata; both reference
  strategies stamp that instant. Exact signal identity/future checks remain
  unchanged. No tolerance window, timestamp rewriting or risk bypass was added.
- Moving-clock worker now passes ingestion, real signal, pipeline, risk, costed
  PAPER entry, independent protection, target exit, journal, audit and API state.
  Fixture economics: 111 units, gross 888, charges 31.44, net 856.56. No real
  market return or broker billing verification is asserted.
- Targeted context/lifecycle regression: **24 passed / 0 failed / 0 skipped**,
  `backend/logs/advancing-clock-lifecycle.txt`. Prior targeted strategy/ingestion/
  adversarial selection also passed. Ruff check/format pass all five touched files.
- Full backend regression with actual browsers: **1526 passed / 0 failed /
  3 PostgreSQL-only skipped**, all 14 disjoint batches exit zero and parent reports
  FULL_SUITE_COMPLETE. Logs: `backend/logs/advancing-clock-full-0.txt` through
  `-13.txt`. Includes full-session parity, adversarial strategy boundaries and
  actual PAPER/option/historical/OOS Edge browser lifecycles. No process was
  restarted because of quiet buffered output.
- Frontend rerun: **34 passed / 0 failed**, production build/typecheck successful;
  `backend/logs/advancing-clock-frontend.txt`. No frontend source changes needed.
- STRAT-004 evidence updated; no additional requirement promotion. Counts remain
  **229 tested / 13 implemented-unverified / 126 partial / 165 not started**.
  Owner README/migration changes remain untouched. PAPER remains default; Groww
  LIVE, external sources/delivery and PostgreSQL recovery remain unverified.
- Exact next task: authenticated, audited cancellation of owner-controlled
  historical jobs through the existing API/dashboard, including actual child
  termination and durable interruption. Preserve unverified orphan reservations;
  do not mistake a new controller for proof an old process has stopped, or publish
  interrupted research as a completed report. Walk-forward cancellation must
  account for its active child rather than leave detached execution.

### Verified checkpoint: integrated historical research cancellation (2026-10-02)

- Existing Backtests controls now call two typed, authenticated cancellation
  endpoints for confirmed local single-run and walk-forward tasks. Explicit
  reason/confirmation, audit-before-action, duplicate request coalescing and
  terminal idempotency are implemented. The response waits for child cleanup;
  disconnected callers do not discard the shielded controller cleanup task.
- Unknown-owner reservations are retained. Individual experiment children cannot
  be cancelled independently of their parent. Owner interruption is distinguished
  from process shutdown; partial research is never relabelled completed.
- Real subprocess tests found two defects during development: cancellation
  exception messages can be lost across a parent/child await (owner attribution
  now uses the controller's explicit request), and cancellation during child
  reservation could detach the already-launched child. The latter is reproduced
  in `backend/logs/historical-cancel-reservation-reproduction.txt` and fixed by
  settling the shielded reservation and reaping its task before parent exit.
- Initial API/actual Edge/recovery acceptance: **7 passed**, then reservation
  race acceptance **8 passed**. Broader regression exposed an additional genuine
  startup race: concurrent cancellation/state audits collided on chain sequence,
  correctly failing closed but preventing the requested cancellation. Diagnostic
  evidence: `backend/logs/historical-disconnect-repeat-0.txt`. Controller state
  and experiment report writes now serialize with cancellation under the existing
  manager lock; audit uniqueness/integrity checks remain intact. The same eight
  cancellation/recovery tests passed three consecutive reruns, including real
  subprocess reservation and disconnected-caller cleanup. Logs:
  `backend/logs/historical-cancel-serialized-0.txt` through `-2.txt`.
- First broad run: **799 passed / 1 failed**, not a verified checkpoint. After
  fixing the audit writer race, final broad regression: **800 passed / 0 failed /
  0 skipped**, exit zero in `backend/logs/historical-cancellation-regression-final.txt`.
  Includes all unit/e2e/safety, historical and walk-forward integration, real
  cancellation/PAPER/historical/OOS browsers and full-session parity. The same
  authoritative process completed after 1459 seconds; it was not restarted.
- Final deferred test-only formatting and cancellation/recovery rerun: **8 passed**
  in `backend/logs/historical-cancellation-format-final.txt`. Ruff passes touched
  Python files. No production source changed after the broad passing regression.
- Frontend **35 passed / 0 failed**, generated contract and production build/typecheck
  successful. Initial build caught unsupported test matcher options, corrected
  before the successful build. Final frontend log:
  `backend/logs/historical-cancellation-frontend-final.txt`. No new pages or mock
  application states; the existing Backtests controls consume actual API state.
- New authenticated endpoints: POST `/api/v1/historical-jobs/{id}/cancel` and
  POST `/api/v1/historical-jobs/experiments/{id}/cancel`. Real Edge acceptance
  verifies launch, owner-confirmed cancellation, durable INTERRUPTED state,
  unpublished report, reload and terminated child process.
- Latest complete-suite baseline remains **1526 passed / 3 PostgreSQL-only skips**
  at `dd60894`; the 800-test selection is an affected regression, not a new full
  suite total. FE-013 remains partial: in-browser parameter editing and safe
  unknown-owner recovery are still pending. No requirement promotion.
- Counts unchanged; PAPER default and Groww LIVE unverified. No orphan recovery,
  external verification or M1/M2 completion claim.
- Counts: **229 tested / 13 implemented-unverified / 126 partial / 165 not started**.
- Exact next task: BT-006 exposure-duration reporting from actual persisted OMS
  fill/position history, through shared metrics, sealed historical reports,
  typed API/export and the existing Backtests view. Handle overlapping positions,
  partial exits, open-at-end positions, incomplete windows and legacy reports
  honestly; do not approximate duration by counting samples or invent annualized
  metrics from inadequate history. Unknown-owner recovery remains fail-closed.

### Owner priority/publication change and real environment audit (2026-10-02)

- Owner now authorizes pushes to `sarbeshtiwari/trading-software`, `main`, using
  the configured author identity only and no co-author trailers. This supersedes
  prior local-only instructions. Empty remote inspected; old local history had
  four co-author trailers, so it remains on local `master`. A clean snapshot of
  verified `07c5695` was published as `b8a1572` on `main`; no force push or original
  history rewrite. Effective identity: the owner's existing repository config.
- Scanned all 673 tracked snapshot files for configured secret matches and
  forbidden private-file names: zero matches. Uncommitted README/migration and
  `prev_chat.txt` remain untouched and unpublished. No credential value was shown.
- Actual configuration has Groww TOTP credentials and capital, SUPERVISED/Groww,
  worker disabled. Defaults remain PAPER; private mode was not changed or armed.
- Installed missing official SDK 1.5.0; `pip check` passes. Added reproducible
  `groww` optional dependency and an explicit read-only diagnostic CLI.
- Actual token authentication succeeds. NIFTY LTP fails with HTTP 403 through
  the real adapter, including its bounded re-authentication. No order requests.
  This is not data freshness or LIVE execution verification. Current official
  documentation: https://groww.in/trade-api/docs/curl and `/curl/live-data`.
- Actual PostgreSQL connection is refused; Redis connection fails. Previous
  PostgreSQL-only tests remain unverified, not retroactively passing. A first
  one-off DB diagnostic used a Python-3.11 timeout API on Python 3.10; corrected
  to `wait_for` before reporting the actual connection refusal.
- Read-only diagnostic plus existing broker regression: **80 passed / 0 failed /
  0 skipped**. External check is reported separately, not counted as a passing
  test. See `APPLICATION_STATUS.md` for the concise owner-facing progress board.
- Priority changes: operational PostgreSQL/Redis and authorized Groww data
  readiness come before further report metrics. Do not auto-start SUPERVISED
  execution, submit real orders, invent missing observations or claim M1/M2.

### Existing Docker services confirmed (2026-10-02)

- Owner clarified PostgreSQL and Redis already run in Docker. Confirmed healthy
  `ats-db-1` (TimescaleDB 2.17.2/PostgreSQL 16) and `ats-redis-1` (Redis 7),
  published only on localhost ports 5432 and 6379 respectively.
- Real connections using application settings now pass PostgreSQL `SELECT 1`
  and Redis `PING`. Earlier connection failures are resolved. No PostgreSQL or
  Redis packages were installed in WSL; only package metadata was refreshed.
- Completed readiness regression: **792 passed / 0 failed / 0 skipped**
  (unit, safety, Groww integration fixtures and application boot).
- Next: verify migration head and application readiness using the existing
  services; Groww market-data HTTP 403 remains unresolved and LIVE unverified.
- Migration verification found the database at `0007_paper_execution_slot`.
  Applied the five existing pending migrations successfully; `alembic current`
  now reports `0012_news_provenance (head)`. No trading worker was enabled and
  no private mode/configuration or owner migration source was changed.
- Exact next task: application readiness/startup verification against these
  services in explicitly non-executing PAPER mode, then diagnose authorized
  Groww read-only data access without sending orders.

### Actual PAPER startup and instrument-source check (2026-10-02)

- Published readiness checkpoint `0240c7e` to configured GitHub `main`; verified
  configured author and no co-author trailer. Unrelated owner changes excluded.
- Started actual Uvicorn on localhost port 8011 using process-only PAPER/paper
  overrides, worker disabled, news polling and outbound notifications disabled.
  Private configuration unchanged. Startup completed against Docker PostgreSQL
  and Redis; unauthenticated HTTP request correctly returned 401.
- Startup correctly refuses trading: instrument master is empty. Startup
  coverage also reports missing news/order-service checks. This is not a ready
  trading deployment or authenticated dashboard verification.
- Existing public Groww CSV download succeeds independently of LTP HTTP 403.
  Existing parser accepts 98,783 rows; rejects 36,501 unsupported COMMODITY rows
  and one missing-identity row. No instrument database writes performed yet.
  This verifies public download access, not live quotes or order execution.
- Next: validate real CSV lot/tick/type/expiry semantics and ingestion safety
  before populating the instrument master; then verify runtime readiness again.
  Diagnostic Uvicorn process is tracked by execution session 77242.

### Instrument master acceptance correction (2026-10-02)

- Real public CSV inspection exposed missing CE/PE side extraction from the
  `instrument_type` column. Fixed the existing parser rather than introducing
  another loader. Missing/malformed/nonfinite lot and tick values now reject
  rows instead of inventing defaults; fractional/oversized lots are rejected.
  Derivatives require expiry; options require side and positive finite strike.
- Targeted parser/store regression: **39 passed / 0 failed / 0 skipped**.
  Existing loader has 25 pre-existing Ruff modernization warnings; no unrelated
  annotation rewrite undertaken. These tests do not certify production import.
- Downgraded GRW-016 from verified to partial: scheduled production refresh and
  real import acceptance were not demonstrated. Counts are now **228 verified /
  127 partial / 13 implemented-unverified / 165 not started** (533 total).
- Next: complete auditable production instrument import, including broker
  tradability restrictions, duplicate handling and raw snapshot provenance,
  verify on PostgreSQL, then rerun actual runtime readiness. No instruments
  imported into owner database yet; no orders, worker activation or LIVE claims.

### Broker instrument restrictions and PostgreSQL acceptance (2026-10-02)

- Existing loader now consumes buy/sell permission and reserved flags. Missing
  or malformed permission metadata is restricted, not treated as permission.
  Either blocked side conservatively prevents new entries; this is not a
  side-specific exit prohibition. Existing owner restriction reasons survive
  refresh. CSV refresh never automatically clears a previously latched catalog
  restriction; audited selective clearance is still pending.
- Duplicate exchange/segment/symbol identities reject the entire import before
  database writes, rather than relying on a database uniqueness failure.
- Focused regression: **48 passed / 0 failed / 0 skipped**. Broader unit/safety/
  market-storage regression: **754 passed / 0 failed / 0 skipped**.
- Actual Docker PostgreSQL acceptance: **1 passed / 0 failed / 0 skipped**, using
  a connection-local temporary table copied from the migrated instrument schema
  and outer-transaction rollback. Verified option side, lot size, restriction,
  idempotent reload and duplicate rejection without changing production rows.
- Requirements remain **228 verified / 127 partial / 13 unverified / 165 not
  started**. GRW-016 remains partial; production snapshot provenance, controlled
  import and scheduled refresh are next. Frontend unchanged; prior 35-test/build
  evidence is not claimed as rerun. External LTP 403 remains unresolved.

### Audited real instrument import (2026-10-02)

- Added immutable instrument snapshot schema/migration 0013, retaining decoded
  source CSV, UTF-8 SHA-256, receipt time, source, parser version and outcome.
  Snapshot, hash-chained audit and catalog changes commit atomically; audit
  failure rolls everything back. PostgreSQL imports serialize with transaction
  advisory locking. Supplied CSV is explicitly distinguished from public download.
- Verified actual PostgreSQL empty-table downgrade to 0012 and upgrade to 0013.
  Actual PostgreSQL isolated import test passes; snapshot/audit tables are also
  temporary, so tests never write synthetic evidence to production tables.
- Real download contained conflicting identities. Default import rejected it
  without writes. Explicit quarantine mode excludes all duplicate-key rows and
  deactivates matching old entries; it cannot be combined with retaining missing
  entries. Original source evidence is retained, never silently deduplicated.
- Real controlled import completed: **98,750 inserted**, **36,535 skipped**:
  30 invalid lots, one missing identity, 36,501 unsupported COMMODITY rows and
  three quarantined duplicate rows. Source contents can change between downloads.
  59,678 catalog rows are conservatively restricted by broker permissions.
- Independently verified persisted count, source checksum and audit integrity.
  Existing diagnostic backend refreshed 98,750 instruments and cleared the
  empty-master failure; market-data gate remains blocked. Private worker stays
  disabled, no market prices/fills were invented and no order requests were sent.
- Reproducible maintenance CLI: `python -m scripts.import_instruments`, with
  explicit `--quarantine-duplicates` when necessary. Scheduling, audited
  restriction clearance and historical point-in-time catalog linkage remain
  partial; this source receipt must never be used before its recorded timestamp.
- Tests before final regression: 49 focused passed, one actual PostgreSQL passed;
  quarantine-specific store regression 22 passed. Frontend unchanged.
- Final affected regression: **776 passed / 0 failed / 3 PostgreSQL-only skips**;
  actual PostgreSQL instrument test was separately run and passed, not counted
  as satisfying the three unrelated skipped schema checks.
- Next: bounded Groww 403 response classification and operational market-data
  readiness; instrument daily scheduling remains pending. No M1/M2 or LIVE claim.

### Safe Groww diagnostics and failed-token budget (2026-10-02)

- Bounded real probe at 11:10:49 UTC: token acquisition VERIFIED; GET
  `https://api.groww.in/v1/live-data/ltp`, API version 1.0, returns HTTP 403
  classified as JSON_BROKER_FAILURE. Configuration source is application settings;
  subscription state UNOBSERVED. No order requests. Vendor/account cause remains
  unknown; no security bypass or repeated indefinite retries attempted.
- Diagnostic only exposes allowlisted classifications, never response bodies.
  Removed non-JSON body previews from exception context to avoid retaining
  reflected secrets. Added HTML/empty/non-JSON/JSON failure regression coverage.
- Fixed failed/cancelled token exchanges not consuming the local attempt budget.
  **86 tests passed / 0 failed / 0 skipped** across diagnostics/auth/Groww fixtures.
  Real 403 is a separately recorded failed external verification, not a passing
  live-data test. Frontend unchanged; no frontend retest claimed.
- AUTH-003 downgraded to partial: the existing calendar-day/process-local guard
  does not prove durable rolling-24-hour protection across restart/processes.
  Counts now **227 verified / 128 partial / 13 unverified / 165 not started**.
- Next: durable shared authentication-attempt budget, followed by scheduled
  instrument maintenance and runtime market-data readiness. The external 403
  does not prevent continuing these independent safety/runtime improvements.

### Durable shared token-attempt reservations (2026-10-02)

- Added migration 0014 and a database-backed rolling 24-hour attempt budget.
  Every authenticator reserves and commits before external token exchange;
  failures/cancellation cannot refund attempts. Database failure stops the
  external call. Cached access-token use does not consume another reservation.
- All updated processes sharing the database use one conservative Groww budget,
  independent of token/credential rotation. PostgreSQL row locking and a version
  check prevent concurrent oversubscription. Restart/midnight cannot erase the
  rolling history; backwards time/corrupt history fail closed. No credentials
  or tokens are stored in the budget table.
- Existing local daily diagnostic counter remains an additional conservative
  limit, not the authoritative shared rolling count. Pre-migration requests and
  requests made outside this database are not observable and are not invented.
  This cannot certify remaining vendor quota or erase the existing HTTP 403.
- Groww/auth suite: **78 passed / 0 failed / 0 skipped**. Added restart/midnight,
  competing reservation and database-failure tests: **3 passed**. Actual Docker
  PostgreSQL independent-connection test: **1 passed**, isolated schema cleaned
  afterward. Migration applied successfully to actual local database.
- Broader unit/safety/reservation regression: **743 passed / 0 failed / 0 skipped**.
- AUTH-003 remains partial pending operational quota acceptance; counts unchanged
  (**227 verified / 128 partial / 13 unverified / 165 not started**). No external
  token/order calls made by this checkpoint's verification. Frontend unchanged.
- Next: scheduled, audited instrument maintenance integrated with application
  startup/session lifecycle, then continue market-data and dashboard readiness.

### Application instrument-maintenance lifecycle (2026-10-02)

- Added opt-in `INSTRUMENT_REFRESH_ENABLED=false` (default unchanged) and
  integrated existing audited loader with APScheduler, application startup,
  graceful shutdown, readiness API and the deterministic entry gate.
- Refresh only occurs from 08:00 to 09:00 IST on calendar-verified trading days;
  an earlier special-session start shortens the window. No intraday catch-up.
  Successful public snapshots are reused after restart after audit verification.
  PostgreSQL transaction advisory locking prevents simultaneous scheduler owners.
- Failed attempts are audited and suppress further automatic attempts that day,
  including after restart. Missing/failed/stopped maintenance blocks new entries
  when enabled; it does not block protective exits. No silent calendar override.
- Actual scheduler ran against configured Docker services with process-only PAPER
  configuration, execution disabled: one registered job, state
  `CALENDAR_UNVERIFIED`, readiness FAIL, clean shutdown. No download or order call.
  This is a truthful blocker, not a completed pre-open production observation.
- Initial broader regression found missing `.env.example` documentation for the
  new flag (754 passed, one failed); corrected before committing. Existing owner
  configuration is untouched and automatic maintenance remains opt-in.
- Final regression: **756 passed / 0 failed / 0 skipped** (unit, safety,
  maintenance integration and application startup). New runtime/test lint passes.
- Counts unchanged: **227 verified / 128 partial / 13 unverified / 165 not
  started**. Frontend unchanged; health state is exposed through existing APIs.
- Next: verify the production exchange calendar against authoritative exchange
  sources and persist its provenance; continue market-data readiness without
  bypassing the unresolved Groww 403 or enabling real orders.

### Source-grounded calendar corrections (2026-10-02)

- Inspected official NSE CMTR71775/FAOP71777 annual circulars, CMTR72260 election
  amendment and CMTR72349 Budget special-session circular. Recorded source URLs,
  publication dates, actual 2026 holidays and the 09:15-15:30 February 1 session.
  Did not confuse settlement-only closures (e.g. April 1) with CASH holidays.
- Added timezone-aware `as_of` loading and per-entry availability filters;
  publication time without an official intraday timestamp conservatively becomes
  next-day midnight IST. Future amendments/special sessions are not injected into
  earlier calendar snapshots. Legacy entries remain explicitly unverified.
- No inferred Muhurat hours: November 8 timing remains unavailable in the checked
  official circulars. BSE/full-segment and 2027 coverage still require verification.
  `complete` stays false, so runtime calendar gates are not weakened.
- Corrected EXCH-001 from verified to partial because the contract explicitly
  requires current AND next-year NSE/BSE coverage. Counts now **226 verified /
  129 partial / 13 unverified / 165 not started**; no requirement removed.
- Focused calendar/session/maintenance tests: **37 passed / 0 failed / 0 skipped**.
  Complete backend suite launched as execution session **78042**, log
  `backend/logs/full-runtime-calendar-checkpoint.txt`; poll, do not restart.
- Next: finish authoritative exchange/segment calendar coverage and date-scoped
  unavailable special-session handling, while full regression runs. Groww data
  403 remains external/unresolved; no orders or fabricated market evidence.

### Operational blockers exposed in existing Monitoring view (2026-10-02)

- Official BSE annual-notice lookup was unavailable; no BSE verification claimed.
  Continued independent runtime/UI work rather than weakening calendar gates.
- Added authenticated typed `/api/v1/system/readiness`, reporting actual gate
  blockers, worker configuration/state, cached health age, calendar warning,
  persisted/restricted catalog counts and latest nonfuture snapshot audit/source.
  It makes no broker calls and never claims that catalog presence permits trading.
- Connected the existing Monitoring page to this endpoint with 10-second polling,
  loading/error/unavailable states and explicit stale health. Failed requests
  clear old displayed readiness rather than retaining a misleading healthy state.
  No additional page or hardcoded trading values. OpenAPI client regenerated.
- Backend API/boot selection: **13 passed / 0 failed / 0 skipped**. Frontend:
  **37 passed**, TypeScript and production build successful. Actual browser
  acceptance for this new panel remains unverified; prior lifecycle browser
  evidence is not claimed as a new run.
- Full backend suite session **78042** is still running; latest observed progress
  9%, no final outcome yet. Do not restart based on quiet buffered output.
- Requirement counts unchanged (**226 verified / 129 partial / 13 unverified /
  165 not started**). FE-015 remains partial. Next: complete runtime acceptance
  and inspect full-suite failures, then continue safe market-data/PAPER operation;
  BSE/calendar gaps and Groww JSON 403 remain explicitly unresolved.

### Browser readiness acceptance and official-SDK comparison (2026-10-02)

- Extended the existing real Edge PAPER lifecycle test, not a separate mocked UI:
  after target/emergency/stale-data paths, Monitoring calls the authenticated
  readiness API and renders actual instrument counts, gate blockers and missing
  snapshot provenance. No fabricated healthy state for fixture catalog rows.
- Browser execution: **3 passed / 0 failed / 0 skipped**, 8 unrelated scenarios
  deselected. Production frontend bundle is the previously successful build;
  no UI source change in this acceptance checkpoint.
- One bounded official Groww SDK 1.5.0 comparison used the new durable token
  budget: authentication succeeds; NIFTY LTP GET still fails with HTTP 403.
  This supports an external authorization problem rather than an adapter-only
  response issue, but does not identify the account/subscription cause.
  No order calls, no prices reported as verified, no raw response/secrets shown.
- Requested nonsecret dashboard subscription/authorization status from owner;
  independent implementation continues. No repeated vendor probes until new
  evidence or authorization state changes. LIVE remains unverified/disabled.
- Full backend suite remains active as session 78042 (latest observed 18%).
  Counts unchanged: 226 verified / 129 partial / 13 unverified / 165 not started.
- Next: continue outstanding PAPER OMS controls/reconciliation through the real
  API/UI path while external market-data authorization and calendar evidence
  remain blocked; inspect full-suite outcome when it finishes.

### Authenticated PAPER entry cancellation (2026-10-02)

- Added typed authenticated `POST /api/v1/orders/{id}/cancel` and controls in the
  existing Orders view. Requires explicit owner reason/confirmation, a running
  PAPER worker, and a PAPER ENTRY order. Protective exits and other modes are
  refused and audited. This does not implement order modification or bulk cancel.
- Owner intent is committed before broker cancellation; stable request IDs bind
  actor/order/reason and replay recorded outcomes without another broker call.
  The worker lifecycle lock serializes cancellation against trading and shutdown.
  Results include actual terminal state/fills, journal linkage and audit chain.
  Partial fills remain protected positions; cancelling an entry is not a flatten.
- Existing supervisor recovers unfinished owner intents after interruption.
  Fixed a discovered defect: a failed cancellation of a young entry could be
  skipped on the next ordinary cycle, clearing its blocker. Persisted failed
  results now force retry/reconciliation before the entry's normal age deadline,
  including after restart. No unknown order is blindly resubmitted.
- Backend unit/safety/affected integration regression: **777 passed / 0 failed /
  0 skipped**. Frontend: **40 passed**, production build successful. Actual Edge
  UI cancellation of a partially filled PAPER entry: **1 passed**, 11 unrelated
  scenarios deselected. Tests also cover auth/refusals, duplicate concurrent
  requests, storage failure, broker failure, recovery and API-visible state.
- Earlier complete suite finished **1568 passed / 1 failed / 24 skipped**. The
  failure was OpenAPI snapshot equality while newer schema artifacts were changed
  during that run. Regenerated contract equality now passes in the regression;
  this is not reported as a passing full-suite run. Opt-in browser/PostgreSQL
  skips are not external verification. Full regression needs a stable snapshot.
- OMS-005 and FE-008 remain partial. Counts: **226 verified / 129 partial /
  13 implemented-unverified / 165 not started**. No market/broker external calls.
- Next: run stable-snapshot full regression; complete safe PAPER bulk-entry
  cancellation and per-order outcomes without cancelling protection, then address
  remaining OMS modification/revalidation and runtime readiness dependencies.

### Atomic bulk PAPER entry cancellation (2026-10-02)

- Added authenticated `POST /api/v1/orders/cancel-entries` and a server-snapshot
  selection in existing Orders controls. Requires the distinct confirmation
  `CANCEL PAPER ENTRIES`. Reports per-order actual status/fill/error/audit linkage
  and explicitly lists excluded protective exits. This is not a kill switch:
  later entries require the existing disable-entry control to remain disabled.
- Parent plan and every child cancellation intent commit in one transaction
  before the first broker action. Child IDs are deterministic; an unfinished
  batch recovers through the existing supervisor. Retrying the original owner
  request reconstructs its outcome without expanding the original target set.
  Existing unfinished cancellations require recovery before creating a new batch.
- Verified atomic storage failure (zero broker calls), interruption before any
  processing, restart recovery, duplicate request replay, and an intentionally
  untracked test order remaining UNKNOWN/blocked rather than reporting success.
  Protective exits are excluded. No production data or broker response fabricated.
- Backend affected unit/safety/integration regression: **767 passed / 0 failed /
  0 skipped**. Frontend: **41 passed**, production build succeeds. Real Edge
  single/bulk partial-entry cancellation: **2 passed**, 11 unrelated deselected.
- Full run 92864 was stopped after confirmed Windows sandbox subprocess failures
  (not an observation timeout). Emergency CLI failures were reproduced as
  `WinError 5` and the same three tests pass with subprocess permissions. That
  interrupted run is not a passing full baseline. Replacement complete suite,
  including opt-in real-browser tests, runs with required permissions as session
  **94518**, log `backend/logs/full-bulk-cancel-checkpoint.txt`. Poll this handle;
  keep source/schema artifacts stable until completion.
- OMS-005/FE-008 remain partial: general/live cancellation and safe repricing or
  resizing are not claimed. Counts remain **226 verified / 129 partial /
  13 implemented-unverified / 165 not started**. Groww 403/calendar blockers remain.
- Next: inspect full-suite outcome and correct actual defects, then implement
  risk-revalidated PAPER modification or explicitly invalidating cancel/replace
  through the existing proposal pipeline; never directly mutate broker quantity
  or price outside deterministic approval/preflight. Finish remaining runtime
  readiness and exchange-calendar evidence in parallel with independent work.

### Full regression and real PostgreSQL fill safety (2026-10-02)

- Stable checkpoint `ccc8ce0` complete backend/browser run finished: **1609 passed /
  0 failed / 5 skipped**, 29m40s, log `backend/logs/full-bulk-cancel-checkpoint.txt`.
  Session 94518 is finished. All opt-in real-browser cases ran. Five skips were
  PostgreSQL checks; inspection found three were still placeholder exceptions.
- Replaced those placeholders with real PostgreSQL/Timescale checks. Actual audit
  UPDATE/DELETE attempts fail at the deployed trigger; the temporary test row is
  rolled back. Hypertable and scheduled 90-day tick-retention catalogs verify.
  Fill tests use uniquely named isolated tables cloned from the migrated tables
  and the deployed trigger functions, never committed owner-market/trade rows.
- The concurrency test genuinely failed before the fix: two transactions could
  each accept 60 fills against one 100-unit order. Added migration **0015**:
  every fill serializes by updating the parent order row before summation. This
  also produces serialization failures rather than accepting stale snapshots at
  REPEATABLE READ/SERIALIZABLE. Order quantity reductions cannot undercut fills.
  Existing inconsistent orders prevent migration. Downgrade deliberately refuses
  to remove these safety guarantees; refusal leaves migration state unchanged.
- Applied 0015 successfully to the existing Docker database. Schema-related run:
  **26 passed / 0 failed / 0 skipped**, including six actual PostgreSQL checks
  (four schema/safety, instrument-import atomicity and shared auth budget).
  Broader unit/safety/OMS/schema regression: **794 passed / 0 failed / 4 skipped**;
  the four opt-in PostgreSQL skips are covered by the separate real-database run.
  Frontend unchanged: prior **41 passed**, build and browser evidence retained.
- DB-006, DB-008 and DB-012 now meet their verified acceptance. DB-005 remains
  unverified for its million-row performance/upsert scope. DB-003 was downgraded
  to partial: the old revision-list test did not prove empty-database migration or
  drift-free metadata. Counts: **228 verified / 130 partial / 10 unverified /
  165 not started**. No Groww/data/notification external verification claimed.
- Next: verify the complete migration chain and `alembic check` on an isolated
  empty database using the existing Docker server; fix genuine migration drift
  without changing owner data or unrelated owner edits. Then resume guarded PAPER
  order modification/cancel-replace through fresh deterministic approval.

### Fresh database installation and honest drift detection (2026-10-02)

- Created a uniquely named disposable database on the existing Docker server,
  ran the entire Alembic chain, and dropped only that test database afterward.
  No container replacement, owner data deletion or new PostgreSQL installation.
- Clean installation depends on the owner's existing correction in migration
  0002: PostgreSQL ENUM with `create_type=False` reuses the baseline enum. Kept
  that correction unchanged and included it because it is now directly related
  and verified, rather than leaving the published fresh-install path inconsistent.
  Unrelated README/chat/line-ending work remains excluded.
- The first real `alembic check` failed: Timescale's `candles_ts_idx` was absent
  from ORM metadata. Represented its descending timestamp index explicitly and
  added idempotent migration **0016** for databases without it. Downgrade retains
  the pre-existing Timescale index rather than deleting earlier-version state.
  No blanket drift suppression or exclusion of application tables was added.
- Actual fresh install, clean drift check, 0016 downgrade/re-upgrade, and a
  deliberately introduced extra-column drift detection all pass (**1 integrated
  scenario**, 27.63s). Existing owner DB upgraded to **0016** and its actual
  `alembic check` reports no new upgrade operations. No credentials printed.
- Backend affected regression: **801 passed / 0 failed / 4 PostgreSQL-only
  skipped**. Those four were separately verified in the previous real PostgreSQL
  checkpoint. Frontend unchanged, prior **41 passed**, build/browser evidence
  retained. Last completed full backend/browser baseline remains **1609 passed /
  0 failed / 5 skipped** at ccc8ce0, not relabeled as a new full run.
- DB-003 now restored to verified with real acceptance evidence rather than the
  old revision-list assertion. Counts: **229 verified / 129 partial /
  10 implemented-unverified / 165 not started**. M1/M2 remain incomplete.
- Next: continue PAPER order modification with an explicitly audited cancel/
  replace contract tied to a fresh approved proposal, rechecking sizing/risk and
  preflight and refusing filled/unknown/protective orders. Preserve original
  proposal/fill lineage; interruption must never blindly submit a replacement.

### Guarded replacement work in progress (2026-10-02)

- Uncommitted implementation adds authenticated PAPER cancel/replace using a
  separate canonical approved proposal, not owner-supplied price or quantity.
  Parent/child intents and proposal reservation are atomic; a replacement
  authorization guard prevents ordinary worker submission after interruption.
  Original and replacement orders retain explicit parent linkage.
- Added candidate/read and replacement APIs, regenerated the typed contract,
  and connected controls in the existing Orders view. UI explicitly discloses
  that cancellation can succeed without a replacement and retains request IDs
  after unknown network outcomes. No additional dashboard page.
- Focused execution/cancellation/workspace regression: **30 passed / 0 failed /
  0 skipped**. This includes replacement through actual approvals/PAPER broker,
  cancellation failure with no replacement, and interrupted reservation recovery
  that blocks automatic resubmission. Frontend: **42 passed / 0 failed**.
- This is NOT a verified checkpoint yet: further adversarial coverage, actual
  browser replacement acceptance and broader regression remain pending. No
  requirement status or completion count is upgraded. Do not commit or push
  this unit as complete until those checks succeed.
- Exact next task: exercise post-cancellation risk changes, filled/unknown
  refusals, stale approvals, storage failure and restart with an accepted
  replacement, then real-browser acceptance and the broader safety regression.

### Guarded PAPER replacement acceptance (2026-10-02)

- Completed the in-progress cancel/replace path using fresh canonical approvals,
  immutable original lineage, atomic reservation/cancellation intents and current
  submit/preflight checks. Owner controls cannot supply arbitrary price/quantity.
  Filled/unknown/stale cases refuse before cancelling. Storage failure rolls
  back both intents and reservation with no broker call.
- Verified risk disarming after cancellation prevents replacement; broker cancel
  failure leaves the request blocked. Restart after accepted replacement but
  before its result receipt reconciles the existing order without duplication.
  Interrupted unused approvals are blocked rather than automatically submitted.
- Backend unit/safety/affected execution regression: **783 passed / 0 failed /
  0 skipped**, one existing serializer warning. Expanded replacement-specific
  run: **9 passed / 0 failed / 0 skipped** (overlapping, not additive evidence).
  Frontend: **42 passed**, production build successful. Actual Edge browser
  replacement plus single/bulk cancellation: **3 passed**, 11 deselected.
  Browser verification observes the dashboard's own authenticated workspace
  refresh and checks replacement quantity and parent order linkage.
- OMS-004 is now partial, not complete: this implements guarded PAPER
  cancel/replace, not general cross-broker amendment. Counts: **229 verified /
  130 partial / 10 implemented-unverified / 164 not started**. Last full baseline
  remains 1609 passed at ccc8ce0; no new full-suite claim.
- Next: stable-source full backend/browser regression, then the remaining PAPER
  runtime readiness and market/calendar gaps. Groww price HTTP 403 and incomplete
  exchange coverage remain external/unverified; no live orders or arming.

- Published replacement checkpoint: **00527ce** on **main**, pushed successfully
  to the existing `origin`; remote hash verified. Author is the configured
  `sarbeshtiwari`, no co-author. Worker startup/review regression also passed
  **6 / 0 / 0**. Unrelated README/chat/test-line-ending changes were excluded.
- Full backend plus opt-in browser regression is running as execution session
  **56875**, log `backend/logs/full-replacement-checkpoint.txt`. Poll that handle;
  do not restart because output is quiet. Keep application/schema source stable.
- Read-only next-dependency inspection identified a calendar correctness gap:
  per-row publication timestamps are filtered, but year completeness is not
  availability-scoped. After the stable regression, address point-in-time
  completeness and date-scoped unknown special sessions without guessing
  exchange times or declaring BSE/2027 coverage verified.

- Read-only reproduction while the full suite runs: an in-memory deterministic
  calendar with year `available_at=2026-01-13` and a matching dated holiday,
  loaded as of January 12, incorrectly reports `is_year_complete(2026)=True`
  with `holiday_count=0`. No production calendar or market data was modified.
  This confirms the point-in-time completeness defect independently of a
  proposed fix. Preserve source until session 56875 completes, then add the
  failing boundary test and fix availability-scoped completeness.
- Bounded official-source follow-up: BSE notice 20251212-8 still returns HTTP403
  through the documentation fetch. NSE's current holiday page still says
  November 8 Muhurat timings will be notified subsequently. No hours inferred,
  no BSE/next-year completeness claim, no additional Groww token/data retries.

- Full-suite observation: session **56875** remains live after six consecutive
  five-minute waits. Progress advanced from 8% to **35%**, with skips but no
  failure markers in the captured progress. This is not a passing result.
  Continue polling the same session; it has not been restarted or terminated.

### Full-suite result and cross-dialect index correction (2026-10-02)

- Session **56875 is finished**: **1618 passed / 1 failed / 7 skipped**, three
  warnings, 56m03s. Do not poll it as running. Log:
  `backend/logs/full-replacement-checkpoint.txt`. This is not a green baseline.
- Failure: SQLite migration roundtrip compared a reflected column index with
  the textual `ts DESC` metadata expression and falsely reported remove/add
  drift. Replaced the textual expression with a structured descending column
  expression; the actual descending index is retained, not excluded from checks.
- SQLite regime/migration regression: **4 passed**. Actual PostgreSQL fresh
  install, drift detection and database-schema/safety selection: **25 passed /
  0 failed / 0 skipped**. No migration rewrite or owner-data change was needed.
  The failing full-suite scenario now passes; no new complete-suite success is
  claimed. Frontend unchanged from 42 passing tests/build/browser verification.
- Requirements unchanged: 229 verified / 130 partial / 10 unverified / 164 not
  started. Next: fix the independently reproduced calendar completeness
  look-ahead defect and verify source availability/session gates.

### Calendar completeness publication gate (2026-10-02)

- Fixed the reproduced look-ahead defect: file-loaded year completeness now
  requires an explicit timezone-aware availability timestamp. A future year
  publication withholds its rows and completeness; undated legacy completeness
  remains unverified rather than enabling trading. Individual amendment cutoffs
  are preserved. Shipped years remain incomplete, with no invented session times.
- Calendar/session/entry-window/actual worker tests: **40 passed / 0 failed /
  0 skipped**. Complete unit selection: **747 passed / 0 failed / 0 skipped**,
  one existing serializer warning. New tests check before/exact publication,
  missing publication and naive timestamps. Existing calendar-file roundtrip
  fixture now declares its deterministic publication timestamp.
- Updated calendar maintenance instructions and EXCH-001 evidence; requirement
  remains partial and counts remain 229 / 130 / 10 / 164. Existing legacy typing
  and global-cache lint findings in calendar.py were not silently relabeled as
  a clean lint result or expanded into unrelated refactoring.
- Next: represent date-scoped unavailable special sessions explicitly and expose
  them through session/monitoring behavior; retain exchange coverage blockers.
  Cross-dialect correction checkpoint **e08c819** was pushed and remote-verified.

### Explicit unavailable special-session hours (2026-10-02)

- Announced special sessions can now retain their date/name/provenance with
  unavailable hours rather than disappearing or falling back to weekday hours.
  Invalid paired/local time bounds fail configuration validation. The known
  November 8 announcement retains null hours; no new exchange timings invented.
- Unknown-hours dates have no executable session bounds. Entry scheduling
  reports `SPECIAL_SESSION_UNAVAILABLE` and actual market health reports
  `SPECIAL SESSION HOURS UNAVAILABLE`; unrelated dates are unaffected. Existing
  position monitoring remains independent of new-entry eligibility. Instrument
  maintenance also handles the nullable schedule without crashing.
- Calendar/session/worker focused run: **45 passed**. Expanded unit and actual
  worker/maintenance regression: **763 passed / 0 failed / 0 skipped**, one
  pre-existing serializer warning. The actual worker test proves no PAPER order
  is created even when the year is otherwise complete and the day is a weekday.
  No new full-suite or browser/UI acceptance claim for this health detail.
- EXCH-003 evidence updated; EXCH-001 still partial because external coverage
  remains incomplete. Counts unchanged: **229 verified / 130 partial /
  10 unverified / 164 not started**. Frontend source/build unchanged.
- Next: inspect and close remaining runtime PAPER preflight/circuit/expiry
  protections using actual OMS acceptance, not a second compliance pipeline.
  Groww live prices/execution and full exchange calendars remain unverified.

### Supplied circuit-band PAPER enforcement (2026-10-02)

- Inspection found quote circuit limits were parsed but not enforced against
  actual order prices. Added a shared supplied-band check to existing PAPER
  entry preflight and fill simulation, not a second decision/risk pipeline.
  Invalid/one-sided bands and out-of-band entry/depth reject before broker
  submission. Simulated slippage cannot generate an out-of-band fill.
- Preflight persists proposal-linked band status, values, observation time and
  entry price. Both bounds absent is explicitly `UNAVAILABLE`, not invented
  percentages or a claim of complete circuit verification. Stored-tick/replay
  preservation, missing-band admission policy and dispatch refresh remain to do.
- Focused circuit/OMS run: **20 passed**; existing constraint/latency/exchange
  selection: **18 passed**; post-refactor circuit selection: **6 passed**.
  Broader unit/safety/OMS/option/replacement regression: **802 passed / 0 failed /
  0 skipped**, one pre-existing warning. Scopes overlap; do not sum counts.
- EXCH-005 moved from not-started to partial. Counts now **229 verified /
  131 partial / 10 implemented-unverified / 163 not started**. Frontend unchanged;
  audit uses the existing API-visible ledger. No new full-suite/live claim.
- Next: persist and reconstruct provider circuit evidence through ingestion,
  stored observations and historical replay, then enforce dispatch-time updates
  and define explicit unavailable-band safety behavior before completing EXCH-005.

### Recover complete provider circuit evidence (2026-10-02)

- Inspection verified that reference ingestion and typed recordings already
  retain full quote fields, including circuit bands. Reused those records rather
  than adding duplicate quote storage or a migration. StoredQuoteSource now
  prefers complete LIVE-origin observations from the existing PAPER ingestion
  audit, verifies their hash chain, and checks identity/provenance/chronology.
  Legacy sampled ticks remain only a fallback without invented band values.
- A failing cutoff test caught UTC-versus-IST SQL comparison behavior on SQLite;
  normalized aware query cutoffs to UTC and reject naive cutoffs. Future records
  are not reconstructed. Recording roundtrip/replay preserves bands and requires
  the real replay publication step. Audit tampering fails closed.
- Source validation now rejects malformed, nonfinite, reversed and partial
  bands before ingestion/recording, using the same pure supplied-band checker.
  Deterministic external fixtures are confined to tests, including LIVE-origin
  parser/source tests; they do not claim a real market response.
- Unit/ingestion/worker/circuit regression: **774 passed / 0 failed / 0 skipped**,
  one existing serializer warning. Final stored-source selection: **4 passed**.
  Counts remain **229 verified / 131 partial / 10 unverified / 163 not started**.
  No full-suite, live-data or frontend-browser claim added.
- Next: revalidate current bands immediately before dispatch, and explicitly
  refuse unavailable bands for market-fed entry authorization while retaining
  truthful historical-source limitations; verify no-broker-call refusal and
  failure/recovery behavior through the existing OMS before upgrading EXCH-005.

### Dispatch-time circuit authorization (2026-10-02)

- PAPER entries now refresh quote evidence immediately before dispatch and
  independently check current bands/provenance/freshness. Circuit receipts name
  PREFLIGHT versus DISPATCH and link dispatch evidence to the order. Refusals
  persist local REJECTED state and reason, reconcile the reservation, and make
  zero broker submissions; restart cannot revive that order.
- LIVE-origin market-fed PAPER entries refuse missing bands explicitly.
  Historical/replay/synthetic unavailability remains disclosed, not certified
  exchange compliance. Protective exits do not acquire the new entry-only gate.
  Dispatch also refuses price/quantity drift from the approved proposal.
- Updated deterministic partial-fill fixtures to supply separate preflight,
  dispatch and broker depth observations. Isolated LIVE-origin worker fixtures
  supply explicit synthetic test bands, never production/vendor evidence.
- Affected lifecycle selection: **55 passed**; paper-evidence/journal selection:
  **7 passed**; circuit-specific selection: **10 passed**. Final broader
  unit/safety/OMS/evidence regression: **795 passed / 0 failed / 0 skipped**,
  one pre-existing warning. Actual Edge replacement/cancellation/journal:
  **4 passed**, 10 deselected. Frontend bundle unchanged; prior 42 tests/build.
- EXCH-005 remains partial for broader-mode/external and historical coverage.
  Counts unchanged: **229 verified / 131 partial / 10 unverified / 163 not
  started**. No full-suite or Groww-live verification claim.
- Next: expiry-day entry/position monitoring. Inspection found configured
  `fno_expiry_entry_cutoff_time` has no consumer outside Settings; implement
  this existing setting through the shared execution/monitoring path rather
  than adding another disconnected expiry helper.

### Configured expiry admission and durable position alerts (2026-10-02)

- Connected the existing F&O expiry cutoff to shared entry-source validation,
  including dispatch. The cutoff is inclusive in IST and validated as local
  HH:MM. Missing/past contract expiry refuses new entries; no exchange weekday
  or settlement price is inferred. Existing minimum-DTE gates remain intact.
- Startup/periodic monitoring records position-linked critical expiry warnings
  and durable notification requests in one transaction, before quote-dependent
  protection checks. Repeated observations do not duplicate a warning; existing
  warning integrity is checked. Future-opened positions are excluded by UTC
  cutoff. No automatic settlement, fabricated exit or external delivery claim.
- Boundary/configuration plus actual option position/approval tests: **6 passed**.
  Unit/safety/option/worker regression: **773 passed / 0 failed / 0 skipped**,
  one existing warning. Tests prove no broker submission after corrected contract
  expiry reaches cutoff, and an actual PAPER option position remains auditable
  when its later expiry warning occurs with stale market data.
- EXCH-006 is partial. Counts: **229 verified / 132 partial / 10 unverified /
  162 not started**. Frontend source unchanged; alerts use existing audit/outbox.
- Exact next task: cancel pending F&O entry remainders at expiry cutoff through
  the existing durable hygiene/recovery service before settlement can create new
  fills. Verify cancellation failure blocks further processing, restart recovery,
  protective-exit exclusion and notification/API-visible state.

### Expiry cancellation and settlement refusal (2026-10-02)

- Pending PAPER F&O entries now enter the existing durable cancellation flow
  at the configured expiry cutoff, even before their generic age timeout.
  Missing/past expiry also cancels pending entries; protective exit orders are
  excluded. Original intent reasons survive retry and interrupted-result recovery.
- Fixed a worker ordering defect: failed cancellation previously allowed the
  monitor to settle pending orders before raising. Worker now refuses that path;
  direct execution monitoring independently runs the same cancellation guard.
  Existing position protection checks and expiry warnings remain available.
- Actual option strategy/OMS tests cover successful cancellation, worker/direct
  monitor cancellation failure with zero settlement calls, restart and interrupted
  audit-result storage. Authenticated journal API exposes the cancellation and
  audited reason without inventing a closed trade or P&L. Advancing to expiry
  correctly expires the test login; API verification obtains a fresh owner login.
- Focused lifecycle regression: **25 passed**. Final API/recovery selection:
  **4 passed**. Expanded unit/safety/OMS/worker/option/cancel/replace/protection
  regression: **850 passed / 0 failed / 0 skipped**, one existing serializer
  warning; log `backend/logs/expiry-cancellation-regression.txt`. Scopes overlap.
  Changed production modules pass Ruff. Frontend unchanged from 42 tests/build.
- EXCH-006 remains partial for broader-mode/external acceptance. Counts remain
  **229 verified / 132 partial / 10 unverified / 162 not started**. No Groww,
  external notification delivery, complete-suite or M1/M2 claim added.
- Exact next task: run a stable full regression including actual PostgreSQL
  acceptance, reconcile stale operational limitations documentation, then address
  the next runtime integration gap identified by that acceptance run.

### Operational limitations reconciliation (2026-10-02)

- Expiry-cancellation checkpoint **79edc42** was committed and pushed to
  `origin/main`, remote hash verified, configured owner author and no co-author.
- Corrected stale limitations statements that claimed no Docker/PostgreSQL/Redis,
  no PAPER costs and missing Groww credentials. Existing infrastructure and
  scoped database verification are distinguished from full deployment acceptance;
  cost estimates and manual fundamentals are distinguished from external evidence.
  Calendar gaps now describe fail-closed admission rather than assumed weekdays.
- DOC-013 is partial, not complete: remaining historical vendor/regulatory detail
  and final audit still require reconciliation. Counts are **229 verified /
  133 partial / 10 implemented-unverified / 161 not started**. No feature or
  external verification was inferred from a documentation change.
- Full backend/browser/actual PostgreSQL regression is running as session
  **58666**, log `backend/logs/full-expiry-checkpoint.txt`. No final result yet.
  Do not restart because output is quiet. Source/tests remain fixed during this
  run; only documentation is changing. Prior focused safety regression: 850 passed.
- Next: observe that exact run to completion, fix any actual failures, record
  the honest full-suite result, then continue runtime integration work.

### Full expiry checkpoint acceptance (2026-10-02)

- Session **58666 is finished**: **1660 passed / 0 failed / 0 skipped**, three
  existing warnings, 1h16m16s. Log `backend/logs/full-expiry-checkpoint.txt`.
  Actual browser and PostgreSQL checks were enabled using the existing Docker
  server; no live broker orders were submitted. Do not poll this session again.
- This full regression replaces the previously non-green 1618/1/7 result. It
  includes the migration correction and expiry-cancellation changes. Source and
  tests remained unchanged throughout the run; earlier quiet periods were not
  treated as failures or grounds to restart the process.
- Fresh frontend verification: **42 passed**, production build successful.
  Session 3954 is also finished. No external market-data, notification delivery,
  profitability or time-based PAPER validation is implied by this acceptance.
- Counts unchanged: **229 verified / 133 partial / 10 unverified / 161 not
  started**. Documentation checkpoint **48f4096** is pushed and remote-verified.
- Next integrated task: expose server-derived risk utilisation and freshness in
  the existing Risk dashboard. Reuse the same monetary-budget functions and
  audited portfolio evidence consumed by risk, with explicit missing/stale/
  configuration-changed states. No invented account or client-side risk maths.
