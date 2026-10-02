# Runtime health enforcement

The FastAPI lifespan starts `HealthWatchdog` before the PAPER worker. It runs
registered, timeout-bounded checks immediately and then after each configured
`HEALTHCHECK_INTERVAL_SECONDS` delay (default 60). Detection latency includes the
check timeout; this is not an independent process monitor or a hard real-time SLA.
Shutdown stops the worker, then the watchdog, before notifications and database.

Critical FAIL, SKIPPED or DEGRADED results block new entries. Missing required
checks also block entries. Exits retain their existing quote, accounting and
execution safeguards; health recovery never clears risk, emergency or worker
review latches. SQLite has no external clock reference and therefore cannot
establish healthy unattended trading merely because its schema exists.

Each observed status transition and its notification request commit together in
the existing audit/outbox transaction. Repeated identical reports do not generate
new requests. A restarted watchdog records a fresh observation, not an assumption
that the previous process was healthy. Database/audit failure blocks entries;
an unavailable database cannot provide durable evidence of its own outage.
The current implementation records transitions, not every heartbeat or a complete
time-range health-history API.

No operational news service is wired yet: its check truthfully reports
`NEWS SERVICE DEGRADED`, without globally disabling non-news strategies. This
does not certify news-dependent strategy degradation/recovery integration. The
reference strategy has no news input. Notification requests are durable, but
external delivery remains unverified.

The PAPER order-service check now reads the active recovered worker, its persisted
cycle heartbeat, unknown orders, current simulated cash/margin and independently
audited protection observations. Disabled execution is explicitly SKIPPED;
enabled-but-absent, stale or unrecovered execution is FAIL. The check is read-only
and never acquires the worker cycle lock, which owner recovery controls may
already hold while requesting health checks. It cannot place orders or clear an
owner-review latch. A PASS establishes runtime availability, not permission to
trade or a fresh independent broker reconciliation.

Health checks retain their other provider capabilities and limitations. All
contract check names are now visible in the running application, but unavailable
news is not a completed news integration. This watchdog is not evidence of Groww
connectivity or live-order verification. Raw exception text is excluded from
health results; the exception class identifies failed checks without exposing
uncontrolled dependency exception payloads.

# Replay availability correction

The recorded-bar provider publishes a candle only at its opening timestamp plus
its interval. All candles closing simultaneously become visible before callbacks.
Multi-instrument advancement never renews older quote timestamps. Invalid,
overlapping or contradictory interval data is rejected; callback failure stops
further delivery rather than silently continuing an incomplete run.

Session-shortened candles, delayed publication, recorded order-book replay and
next-bar execution simulation are not modeled here. No depth or live prices are
invented. This correction is necessary for backtesting, not a completed backtest
engine or full-day PAPER replay certification.

## Provider-health evidence and durable transitions

Health transitions now use a verified per-mode audit chain, so restarting the
watchdog does not enqueue unchanged conditions again. Actual FAIL results for
authentication and market data create typed CRITICAL notifications. Recovery to
PASS creates INFO notifications even through intermediate DEGRADED/SKIPPED states.
Missing PAPER Groww credentials remain SKIPPED, not fabricated authentication
failure. Recovery leaves independent risk and emergency gates intact.

Market-data health no longer defaults missing/invalid provider status to success.
The live adapter reports unavailable before connection/observations and after
close; stale or future tracked observations fail health. Connected, fresh REST
fallback is DEGRADED, not falsely labelled websocket health. Nonfinite/nonpositive
feed prices do not renew freshness. This is conservative tracked-observation
health, not external proof of all exchange data or exchange timestamp fidelity.

Runtime wiring remains unfinished: startup currently constructs a separate data
provider from the worker. This unwired provider correctly remains unavailable.
The next integration must bind health to the selected worker provider and refresh
read-only data without granting entry permission or deadlocking behind the health
gate. DEGRADED REST policy must retain explicit fresh-input evidence and truthful
UI status. No external Groww connection or live order is verified by these tests.
