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

ARCH-016 and ERR-006 remain partial. Runtime producer/outbox integration and
durable alert linkage are pending.
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
offered. Durable audit/notification publication is the next integration step;
neither this read API nor a visible alert proves external notification delivery.

Acceptance includes actual Redis failed-handler retention, authenticated API
access, metadata non-disclosure and actual Edge rendering. Test keys are uniquely
prefixed; production streams are never deleted or overwritten by these tests.

Reference semantics: [Redis XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)
and [Redis XACK](https://redis.io/docs/latest/commands/xack/).
