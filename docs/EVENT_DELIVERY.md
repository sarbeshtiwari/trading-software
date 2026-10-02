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
the publisher state separately. Runtime dashboard consumers, fill/position-specific
events and the remaining event families are not complete merely because order
events now reach Redis. No event is permission to bypass deterministic execution.

Fresh PostgreSQL upgrade/downgrade/schema checks pass on a disposable database;
the additive migration is applied to the existing database. Do not downgrade a
production database with pending intents without an explicit preservation plan:
the downgrade removes this table. Tests exercise actual OMS/provider fixtures,
Redis lost-reply/restart behavior and rollback before broker submission.

Reference semantics: [Redis XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)
and [Redis XACK](https://redis.io/docs/latest/commands/xack/).
