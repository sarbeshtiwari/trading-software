# Notification delivery components

`configured_channels(Settings)` creates only configured channels and warns when
configuration is absent/incomplete. Existing environment settings select Telegram
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) and SMTP (`SMTP_HOST`, `SMTP_PORT`,
`SMTP_FROM`, `SMTP_TO`, paired `SMTP_USER`/`SMTP_PASSWORD` when authentication is
needed). No credentials, recipients or successful deliveries are invented.
Runtime delivery is opt-in with `NOTIFICATION_ENABLED=true`; default is disabled.
`NOTIFICATION_ROUTES` is a JSON mapping of channel names to severity arrays
(default: CRITICAL to Telegram and email). Optional `NOTIFICATION_QUIET_START`
and `NOTIFICATION_QUIET_END` are IST wall times; blank values disable quiet hours.
Queue/retry/timeout/suppression settings are listed in `.env.example`.

Telegram uses HTTPS POST with plain text, no markup parsing and disabled link
previews. Success requires a provider acknowledgement with a message identifier.
See the [Telegram sendMessage contract](https://core.telegram.org/bots/api#sendmessage).
SMTP requires verified STARTTLS before login/message transmission and rejects
recipient refusal; it never falls back to plaintext. Port 465 implicit TLS is not
implemented. See [Python SMTP STARTTLS](https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.starttls).
Transport errors are sanitized, credentials registered with existing log redaction,
and notification snapshots redacted before enqueue. Application logging must use
the existing redacting handlers, including for third-party HTTP debug output.

Construct `NotificationService` with these channels, a `RoutingPolicy` and a
`DeliveryPolicy`. Call `start`, then nonblocking `submit`, and finally `stop`.
Submission validates aware timestamps and rejects future events. The return value
`QUEUED` is **not** delivery confirmation. Routing uses the injected delivery clock
and IST quiet hours, including midnight wrapping; CRITICAL bypasses quiet hours
but never invents an unconfigured channel. Empty routing selects no recipients.

A bounded queue and bounded timeout/retry policy isolate transports from callers.
Channels execute concurrently for each message. Outcomes explicitly distinguish
ACKNOWLEDGED, FAILED, DISABLED, NOT_ROUTED and CANCELLED. Acknowledgement means
provider/server acceptance, not proof the owner read the message. Queue overflow
is logged and returns QUEUE_FULL; it is never reported as successful delivery.
Shutdown records cancelled queued/in-flight events; restart is supported.

Condition suppression and admission rate limits use the injected monotonic clock.
`SuppressionPolicy` bounds the window, admitted events and retained conditions per
severity. Conditions hash event type, severity and explicit `condition_key` (or
message), not event IDs or timestamps. Severity escalation has an independent
budget. Supply an instrument/account-specific key when distinct conditions must
not merge. No external text is interpreted as an instruction.

Repeated conditions return SUPPRESSED and increment both per-condition and
cumulative counts exposed by `service.limiter`. A window expires at equality and
does not extend on repetitions. Rate/capacity denial returns RATE_LIMITED or
TRACKING_FULL. Active keys are never evicted just to admit new conditions. Queue
failure does not consume an admission. State is bounded and process-local, not
an exactly-once guarantee across restart; shutdown/restart of the same service
retains suppression until expiry. Delivery failures still use bounded retries;
new submissions of the same condition can retry after the window expires.

FastAPI startup creates the configured worker, and shutdown cancels it before
database disposal. Invalid routing configuration disables remote delivery without
weakening local safety. Risk daily-loss/drawdown/engine-error transitions enqueue
CRITICAL messages only after their audit transaction commits, using its event ID.
Storage failures emit a distinct degradation alert without pretending a breach
record was committed. Missing/failed notification services cannot enable entries.

PAPER order state/rejection/stop-fill events and owner emergency activation now
write `NOTIFICATION_REQUESTED` audit records in the same transaction as their
source event. Requests link source audit, order, proposal and position IDs. Stable
logical keys prevent repeated synchronization from enqueueing duplicate notices.
The existing channel abstraction sends them from a separate background outbox
worker, not from the trading cycle. Disabled/unconfigured channels leave requests
pending; no success is invented.

Each channel attempt is durably claimed before transport. Receipts record FAILED
or ACKNOWLEDGED, and acknowledged channels are skipped on restart. Retries use
the configured bounded attempt/timeout/delay policy; counts survive restart.
An interrupted claim waits for its timeout plus a safety interval before another
attempt can be made. A crash after provider acceptance but before recording the
receipt can cause a duplicate notification; this is not exactly-once delivery.
First admission uses the existing rate/condition limiter; retries remain bounded
independently. Quiet hours and routing still apply. Audit corruption prevents
delivery and requires review rather than sending altered content.

The existing Monitoring view exposes recent pending requests, per-channel
acknowledgements/failures, attempt counts and source IDs from `/api/v1/workspace`.
Acknowledgement is explicitly marked **not externally verified delivery**.
Notification failures remain isolated from trading execution; source/audit database
failures retain the normal fail-closed persistence behavior. Shutdown first asks
the outbox to stop gracefully, then bounds the wait, leaving unfinished claims
auditable. This avoids interrupting ordinary SQLite connection startup at teardown.

Limitations: other mandatory producers still use the non-durable in-memory path
or remain unconnected; remote channel validation is pending. Retry exhaustion has
no owner requeue action yet. Suppression state is process-local and receipt claims
are not a substitute for external provider idempotency. Historical ledger scanning
is paginated but not a high-volume distributed delivery system.
The legacy in-memory outcome history is bounded to 1000 entries, not a durable audit log.
Ambiguous timeouts may duplicate notifications on retry. SMTP runs in a thread;
cancellation cannot forcibly terminate an already running SMTP operation, whose
socket operations retain their own timeouts. Do not infer exactly-once delivery.
Use the service on its owning asyncio loop, not cross-thread.

### Durable PAPER incidents

Reconciliation failures now record a hash-chained incident and CRITICAL outbox
request after the failed reconciliation transaction has rolled back. Repeated
failures, including after restart, retain one active incident. A successful
reconciliation records recovery; a subsequent discrepancy starts a new audited
transition and notification. Recovery never overrides independent trading gates.

Worker failures and stale/invalid/missing monitored quotes also emit durable
CRITICAL incidents after the failed worker heartbeat has been persisted. Owner
worker review resolves these incidents in the same transaction as the review;
there is no automatic worker re-arm. Every transition includes its audit identity
so suppression cannot hide a distinct recurrence. Audit/outbox storage failures
block entries; delivery failures remain isolated by the existing outbox.

These notifications appear in the existing Monitoring workspace state. Their
transport fixtures prove delivery logic, not real email/Telegram receipt. Complete
provider-level feed-outage coverage and remaining mandatory producers still
require integration work.

### Durable risk breaches

Daily-loss, drawdown and engine-error latch transitions enqueue CRITICAL notices
in the same transaction as their authoritative risk audit. Delivery being disabled
does not discard the request; a restored outbox can deliver it later. Re-observing
an already latched condition does not enqueue duplicates. A fresh breach in a new
IST session produces a new daily-loss alert even if the prior day was latched.
The source audit ID distinguishes separate breaches during delivery suppression.

Risk audit/outbox persistence failure rolls back both and fails closed with
`RISK_SAFETY_UNAVAILABLE`; it cannot approve an entry. Storage-unavailable alerts
still use best-effort logging/in-memory delivery because that failed database
cannot honestly be claimed as durable storage. Remote transport failure cannot
clear or re-arm a latch, and no remote delivery is claimed by the fixture tests.

Routing/failure isolation is tested with isolated transport fixtures. No remote
Telegram or email message was sent or claimed during development. Risk safety
still emits durable CRITICAL audit events/local logs independently; configured
remote delivery is additional, not a replacement. PAPER stays default.

### Corrupt-request isolation

An invalid notification payload or failed audit-chain verification is withheld
without sending or acknowledging it. The dispatcher logs the request ID only and
continues to independent requests; one corrupt entry cannot suppress later stop,
exit or risk alerts. Cancellation and database errors still propagate to the
supervisor rather than being mistaken for a corrupt individual request.

The Monitoring API reports `INTEGRITY_FAILURE`, unavailable event/source metadata
and no trusted delivery outcomes for the affected record. It does not expose
unverified payload text or rewrite the audit evidence. Restarts retain this
behavior; valid acknowledged requests are not resent. These checks use the actual
PAPER lifecycle and isolated transport fixtures, not external delivery evidence.
