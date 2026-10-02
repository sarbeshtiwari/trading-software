# PAPER event calendar controls

The existing Risk view publishes owner-reviewed event evidence through authenticated
`GET /api/v1/risk/event-controls`, `GET /api/v1/risk/event-controls/{instrument_id}`
and `PUT /api/v1/risk/event-controls/{instrument_id}` APIs. Read-by-instrument requires
an explicit `origin`; optional `strategy_id` reports applicability to that strategy.
This is manual evidence admission, **not an externally verified calendar feed**.

## Publication contract

Use the generated OpenAPI `EventControlChange` schema. Required inputs include
`origin`, strict `enabled`, explicit `max_age_seconds`, a substantive `reason`,
the reviewed `expected_event_id` (null for the first publication), and confirmation
`PUBLISH PAPER EVENT CONTROL` or `DISABLE PAPER EVENT CONTROL`.

Enabled controls require `calendars` using the existing `EventCalendar` model,
or `corporate` evidence using the existing `CorporateCalendarEvidence` model.
Corporate evidence additionally requires explicit `before_seconds` and
`after_seconds`. The existing corporate calendar store and audited control commit
in one transaction. Sources, knowledge dates, coverage, events and original
corporate evidence remain reconstructable. Future event dates are permitted;
future knowledge is not. No windows, freshness limits or event data are invented.

Controls apply to one exact instrument and data origin. Empty
`restricted_strategies` means every strategy; otherwise only the named strategies
are affected. Strategy identity cannot be omitted to bypass restricted scope.
Each supplied calendar must cover the decision time and satisfy its declared
freshness limit. Applicable high-impact event windows are inclusive at both ends.
Corporate results and board meetings use the existing high-impact classification.

## Enforcement and recovery

Server-held controls are checked independently of caller `event_blackout` flags
and strategy optional-input declarations. Quantitative and advisory proposals
share the same veto, after advisory receipt validation. Controls are checked again
in approval, preflight and final PAPER entry dispatch. Approved proposals and
orders archive the reviewed calendar state. Rejections preserve named reasons
and evidence. Existing stop, target and emergency exits remain available.

States distinguish `UNCONFIGURED`, `DISABLED`, `CLEAR`, `BLACKOUT`, `UNAVAILABLE`
and `NOT_APPLICABLE`. Unconfigured is not proof that no events exist. Blackout
and unavailable configured calendars veto entries. Corrupt evidence and current
clock regression fail closed; explicit historical reads select only the known
audit prefix. Restart uses persisted evidence rather than an in-memory flag.

The Risk view polls actual state while retaining a separate reviewed mutation
version. Concurrent writes require a fresh review; failed writes invalidate the
editor. Disabling a calendar does not release unrelated risk latches or blocks.

## Advisory context

The shared advisory service resolves configured controls from storage before
reserving a remote call. Caller-supplied calendar assertions are discarded.
The typed calendar snapshot travels inside escaped `UNTRUSTED_DATA` and the
sealed request audit, including future scheduled events that were already known.
It is context only: the provider cannot change entry controls or authorize orders.
Unconfigured controls are omitted as null rather than inventing a clear calendar.

Receipts retain the original request snapshot and validate instrument, origin and
knowledge timestamps. A newly active blackout is independently evaluated by the
current deterministic pipeline, without rewriting receipt-bound decision inputs.
Historical/replay routing still bypasses remote models and discards caller calendar
assertions rather than reading a present-day calendar into a historical request.

## Limitations

No external feed, exchange F&O ban list, derivative-family propagation or
PostgreSQL cross-process race verification is claimed. These controls do not
replace the existing reference-strategy/regime calendar inputs. Upstream source
refresh and policy coverage remain the owner's responsibility. PAPER stays the
default; Groww LIVE remains unverified. RISK-014 remains partial.
