# Redis event delivery and recovery

The existing `RedisStreamEventBus` now checks pending work with XAUTOCLAIM
before reading new entries. Reclaim idle time defaults to sixty seconds;
handlers have a five-second timeout and bounded attempts. Register stable,
unique handler names before starting the bus. Repeated start is idempotent.

Each stream/group/entry has a Redis receipt hash. Attempt counts are incremented
before handler invocation and successful delivery is recorded before proceeding.
Recreating a consumer does not reset its attempt budget. A crash after a side
effect but before its receipt can still redeliver the event: consumers must be
idempotent. Reclaiming an idle message can overlap a slow former consumer; this
is at-least-once transport, never exactly-once order authorization.

After exhaustion, raw event evidence, stream/group/entry, handler and a safe
failure classification are retained in `<prefix>:dead-letter` before the handler
is marked complete. Decode failures, including invalid byte sequences, are also
retained before acknowledgment. Dead-letter storage failure leaves the original
pending. Error logs do not include raw payloads or exception messages.

The original is acknowledged only after all registered handlers have delivered
or their failures are retained. Receipt cleanup follows acknowledgment. Crash
windows can leave an orphan receipt or duplicate dead-letter record; neither is
silently certified as exactly once. No automatic stream trimming or owner-key
deletion is introduced. Redis persistence/retention policy and operator cleanup
still require deployment acceptance; consumer recovery is not Redis-server
disaster-recovery certification.

Tests use unique temporary prefixes on the existing Redis server, never FLUSHDB
or replacement containers. They cover interrupted consumer recovery, continued
delivery to healthy handlers, poison-event retention, retention failure leaving
pending work and retry budgets across consumer recreation. Enable them with
`ATS_TEST_REDIS_URL`; use the configured URL securely, not pasted into logs.

## Remaining integration

ARCH-016 and ERR-006 remain partial. Runtime producer/consumer integration and
complete operational acceptance remain pending.
Do not connect non-idempotent order placement directly to this transport. The
existing deterministic risk/OMS workflow remains authoritative and unchanged.

## Authenticated monitoring

The existing Monitoring view reads `/api/v1/events/failures`. This endpoint
reads the configured Redis server's default event dead-letter stream; arbitrary
Redis keys are not accepted from the browser. Count and latest fifty records are
read atomically, within an overall six-second deadline. Connection allowance is
five seconds to accommodate the observed Windows localhost connection delay.
Missing Redis or invalid stream state is unavailable, never a fabricated zero.

Only receipt ID, recognized event type, original stream entry ID, a hashed
handler reference and bounded failure classification are exposed. Raw event
payloads, arbitrary handler labels and exception messages remain withheld. The
UI polls every ten seconds, clears failed reads, labels truncated history and
does not equate an empty dead-letter stream with healthy runtime delivery.
This is read-only: no replay, acknowledgment, deletion or trading control is
offered. Neither this read API nor a visible alert proves external notification delivery.

Acceptance includes actual Redis failed-handler retention, authenticated API
access, metadata non-disclosure and actual Edge rendering. Test keys are uniquely
prefixed; production streams are never deleted or overwritten by these tests.

## Audit and notification publication

Application startup starts a separate bounded background publisher. It reads at
most fifty retained records per cycle and persists a sanitized audit record and
critical notification request using the existing outbox. It does not send directly
to a remote channel. Its operation runs with a ten-second deadline and five-second
poll interval; shutdown cancels it and releases its owned Redis connection.

The last verified audit-chain entry is the cursor. Cursor progress, failure
evidence and notification requests commit in the same database transaction. A
failed enqueue rolls the entire batch back; restart reads the same source entries
again. Concurrent cursor changes fail rather than overwrite. Invalid/tampered or
future cursor evidence stops publication. Read APIs never advance this cursor.
The cursor is scoped by configured Redis identity, stream and monitoring mode.
That monitoring mode is not verification of an original event's trading mode.

Monitoring exposes NOT_RUNNING, STARTING, PUBLISHING or
EVENT_PUBLICATION_UNAVAILABLE for this publisher independently of retained counts.
PUBLISHING is audit/outbox operation, not proof of remote delivery or fully wired
runtime events. Remote notification configuration and routing remain authoritative.
Integration tests verify actual Redis plus PostgreSQL temporary-table rollback,
restart idempotency, tamper rejection and bounded background lifecycle. Runtime
Strategy and broader operational event production remain integration dependencies.

## Transactional PAPER OMS publication

Migration `0017_runtime_event_outbox` adds a publication-intent table referencing
the immutable source audit row. PAPER order creation, submission, synchronization
and unknown-state audits insert an intent in the same transaction. Failure to
insert an intent rolls back that state transition; an initial creation failure
prevents broker submission. Earlier audit rows are not silently backfilled.

A separate PAPER-only lifecycle publisher reads committed pending rows, locks
each publication intent, verifies its source audit chain and emits through the
existing Redis bus. It records publication only after Redis accepts the event.
Redis failure leaves the intent pending. If Redis accepts but its reply/database
receipt is lost, replay uses the same event ID; downstream consumers must dedupe.
Audit chain/sequence/hash and order/proposal/position identities accompany events.
Concurrent delivery ordering is not guaranteed; consumers must reconcile current
state rather than treating an event as an instruction to execute an order.

Publication is bounded to fifty intents per cycle, with network/cycle timeouts.
Redis unavailability does not invent delivery or broker success. Monitoring shows
the publisher state separately. The dashboard order consumer is described below;
fill/position-specific events and the remaining event families are not complete
merely because orders reach Redis. No event permits bypassing deterministic execution.

Fresh PostgreSQL upgrade/downgrade/schema checks pass on a disposable database;
the additive migration is applied to the existing database. Do not downgrade a
production database with pending intents without an explicit preservation plan:
the downgrade removes this table. Tests exercise actual OMS/provider fixtures,
Redis lost-reply/restart behavior and rollback before broker submission.

Reference semantics: [Redis XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)
and [Redis XACK](https://redis.io/docs/latest/commands/xack/).

## Authenticated dashboard invalidation

`/api/v1/workspace/stream` shares the quote socket's Origin allowlist, connection
cap and bounded first-message authentication. No token is accepted in a URL.
Session validity is checked before every send, including heartbeat messages.
The connection tails fixed order/fill/position streams, then requests a fresh workspace
HTTP read on initial connection and new events. Every browser reads all events;
these ephemeral observers do not compete in a consumer group or acknowledge OMS
work. Reconnect reads current API state rather than attempting to replay account
mutations. Event payloads never become displayed account values or broker commands.

Repeated event identities are suppressed in a bounded 1,024-entry window. Older
duplicates can cause harmless extra reads. HTTP requests are serialized/coalesced
and bounded; responses from a previous login generation are discarded. Existing
ten-second polling remains active for other state and transport fallback. The UI
labels disconnection/staleness, makes bounded reconnect attempts and refreshes an
expired access token before retrying. Redis failures close the stream, not claim
connectivity. This is not a sub-second all-event delivery guarantee or external
market-feed verification. Protection, price marks and operational changes not
listed below still rely on HTTP polling unless accompanied by committed events.

## Fill and position transition publication

New immutable `PAPER_FILL_RECORDED` audit facts receive an outbox intent in the
same transaction as the fill, FIFO and position accounting. Each newly observed
filled-quantity change also records a linked `PAPER_POSITION_UPDATED` or
`PAPER_POSITION_CLOSED` fact and intent. Position closure, its journal and event
intent therefore commit together. Re-reading the same broker fills produces no
additional fill/position event; order synchronization observations remain distinct.
The publisher uses the same audit checks, stable identities and retry semantics
as orders. Consumers receive lineage identifiers, not instructions or trusted
account deltas. Older historical fills are not silently backfilled.

Tests exercise partial entry, process recreation, emergency exit, duplicate
synchronization, and fill-intent insertion failure after broker acceptance.
The latter rolls back local accounting; recovery imports the accepted fill once
without resubmitting a broker order. Actual Edge acceptance subscribes only to
fill/position streams with polling disabled and observes entry and closure.

## Risk and health changes

PAPER risk-latch changes and successful authenticated re-arm requests now insert
intents with their existing immutable audit transactions. Repeated observations
without a latch change do not publish another risk-state event. Health transitions
insert an intent atomically with their audit and notification requests; repeated
unchanged health checks remain deduplicated. Failure to persist either intent
rolls the transaction back and the existing safety path blocks new entries.

The runtime publisher allowlists both the event phase and its expected source
actor. Re-arm receipts must match the configured owner identity; changing that
identity with pending receipts requires explicit operator review if publication
fails validation. Streams never grant re-arm authority. These non-execution
events do not claim simulated fills or broker connectivity.

The dashboard invalidates current workspace state on these streams. An open Risk
view also refreshes its own API-backed latch state, while preserving unsaved
configuration drafts. Requests are bounded, superseded reads are discarded and
failed reads clear displayed latch state rather than presenting it as current.
HTTP polling continues to cover transport outages and other control families.
Existing deterministic entry gates act independently of notification/stream
delivery. A committed notification request is not proof of external delivery.

## Historical health inspection

Owner-authenticated `GET /api/v1/health/history` exposes the current mode's audited
health transitions in an inclusive UTC interval. Defaults are the latest 24 hours;
explicit timestamps must include an offset, cannot be future-dated, and may span
at most 31 days. Pages contain at most 100 observations, plus the last observation
before the interval where available. Pagination pins both interval endpoints.

The API verifies audit hashes, chain continuity, actor/mode, schema and timestamp
ordering before returning observations. It never runs health checks, writes state,
or changes trading permission. Empty intervals are not reported as healthy; the
preceding observation is context, not proof of uninterrupted service. Existing
Monitoring provides interval selection, pagination and unavailable/error states.

Reads have an eight-second bound and a 10,000-record chain-verification ceiling.
Exceeding that ceiling returns explicit 413 rather than truncated evidence; an
archive/checkpoint verification path for larger histories remains pending.
Malformed/tampered evidence returns 409; storage failure returns sanitized 503.
There is no claim that unobserved outages or process downtime are reconstructed.
