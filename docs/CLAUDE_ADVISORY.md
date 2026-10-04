# Optional Claude advisory tasks for the reference PAPER signal

This integration reviews the existing quantitative hypothesis; it does not add a
strategy, invent market inputs, grant LLM availability to dependent strategies,
or authorize an order. The default remains deterministic PAPER without paid calls.

## Actual path

The reference worker ingests data, computes its existing analysis and signal, and
runs shared proposal validation to resolve persisted point-in-time evidence.
When enabled, the Claude service receives that signal and the resolved evidence
snapshots through the Messages API. It can return `CONTINUE` or `ABSTAIN`, a
rationale and supplied source IDs. It cannot return prices, size, confidence,
risk overrides or broker instructions. Unknown fields, duplicate JSON keys,
unresolved citations, malformed JSON, refusals and incomplete responses are not
accepted as reviews. Numeric proposal fields still come from the original signal.

The resulting proposal goes through the existing validation, independent sizing,
risk and execution preflight. Wall-clock quote freshness is rechecked after the
review. Occupied PAPER lifecycle slots skip reference evaluation; each remote
reference-review wait is additionally capped at the smaller of the configured
LLM timeout and worker cycle interval. Optional review must not monopolize the
worker's heartbeat cadence. The worker's durable cycle claim prevents replay after restart. A valid
abstention produces a negative candidate rather than an order. Provider failure
falls back to the original deterministic hypothesis; no LLM dependency is waived.
Historical and replay origins never use remote review, preventing current model
knowledge from influencing historical results.

Source-ID membership and immutable quantitative fields provide capability
containment, not proof that a model's prose is factually correct. The rationale
is advisory text, not independently verified research. No arbitrary tool calls
or assistant-message prefills are sent. System instructions and escaped untrusted
JSON are separated; the versioned prompt has a pinned content hash.

## Bound proposal task

`LLM_REFERENCE_TASK=review` preserves the original review path described above.
With paid review explicitly enabled, `LLM_REFERENCE_TASK=proposal` instead uses
the registered `grounded_trade_proposal` version `1.0.0` task. Its declared inputs,
output schema, instructions and failure policy are archived with the exact request.
The task can abstain or return the existing typed proposal schema. Instrument,
direction, prices, quantitative confidence, evidence and exit rules must match the
supplied hypothesis. Quantity remains advisory: independent sizing is mandatory.
Missing or changed evidence and unsupported numeric output are rejected. This
initial numeric check is conservative, including rejection of comma-formatted
numbers; it is not semantic verification of all prose claims.

LLM-origin decisions now require an opaque persisted call receipt, not a raw JSON
payload plus `llm_available=True`. Receipt resolution verifies the request/result
audit chain, response hash, parsed output, task identity, mode, cycle and exact
decision context. It rejects future/stale receipts and changed source snapshots.
Availability is derived only for that validated proposal, not globally granted
to other strategies. The receipt is atomically claimed once and rechecked inside
the proposal transaction. A failure rolls back the claim and proposal together;
committed consumption survives restart. Repeated or concurrent claims cannot
reuse that receipt to create another proposal.

LLM-dependent strategies cannot substitute a plain QUANT signal and availability
flag for a receipt. The archived signal must also match the decision's registered
strategy version, timeframe, instrument, product, provenance and timestamp.

Provider failure keeps the existing optional QUANT fallback. ABSTAIN produces no
order. LLM-dependent strategy prerequisites are not bypassed. Historical/replay
origins never invoke the remote task. Default review remains QUANT-origin because
its numeric proposal is produced locally; a successful proposal task is explicitly
LLM-origin and linked to its call. Both converge on the same deterministic gates.

The existing Decisions API/view exposes the actual task receipt and proposal
link. No additional page or direct model-to-broker route is introduced. This is
not yet a general news/research synthesis or parallel multi-agent implementation.

## Configuration and Windows setup

The existing Market / F&O view now has an API-backed source-configuration editor.
`GET /api/v1/news/sources`, `GET /api/v1/news/sources/{slug}` and
`PUT /api/v1/news/sources/{slug}` require owner authentication. Mutations remain
PAPER-only and require a reason plus the last audit event ID for an existing
audited configuration. Configuration and audit commit together; stale concurrent
updates, audit failure and changed unaudited configuration cannot silently win.
Set `is_enabled=false` to disable a source without discarding its policy history.
Unmanaged legacy rows are identified rather than presented as audited policies.

Supply actual publisher identities, source kind/tier and a credential-free HTTPS
endpoint. Credentials, queries and fragments are not accepted in configuration
URLs. Configured weights are recorded; complete source-weighted news orchestration
is not implemented. Endpoint configuration does not perform a request or verify
DNS/egress safety, availability, publisher independence or content correctness.
The acquisition boundary applies network safety checks when a poll is requested.
The API reports `SUPPORTED_ADAPTER` for supported source kinds, not connectivity;
actual attempts and scheduler state are exposed separately.

Stored NEWS evidence is subject to an independent corroboration gate before it
can reach a proposal or stored-sentiment consumer. A `VERIFIED` flag or claimed
source count is insufficient. The article must have explicit data provenance,
body text and point-in-time timestamps. Every attribution must resolve to an
enabled configured source with an explicit independent `publisher_id`, a matching
HTTPS hostname and a non-future fetch timestamp. Two aliases of the same publisher
count once. Admissibility requires two independent publishers or a configured
Tier-1 `filings`/`regulator` source. Conflict, stale, malformed, future-configured,
unknown-origin and mixed-origin evidence is unavailable. Configured source tiers,
not article-supplied tiers, determine credibility in stored sentiment.

Migration `0012_news_provenance` adds nullable provenance fields without inventing
values for existing records. Legacy news remains unavailable until genuine source
configuration and data origin are supplied. Downgrade refuses to discard recorded
provenance. This does not verify publishers against external services or implement
story deduplication or automatic entity mapping. Those remain
pending before automatic news-to-worker research assembly can be enabled.

First apply the new migration with the existing environment's Python:

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Keep `LLM_REFERENCE_REVIEW_ENABLED=false` unless the owner deliberately enables
paid requests. Enabling requires `LLM_PROVIDER=claude`, an actual secret supplied
through `ANTHROPIC_API_KEY`, and all of:

- `LLM_TARIFF_MODEL`: must match the configured supported model.
- `LLM_INPUT_USD_PER_MILLION` and `LLM_OUTPUT_USD_PER_MILLION`: owner-confirmed rates.
- `LLM_TARIFF_VALID_UNTIL`: timezone-aware ISO timestamp for that tariff's validity.
- Sufficient `LLM_DAILY_COST_CAP_USD` and `LLM_DAILY_TOKEN_CAP` for a reservation.

Blank tariff fields mean UNAVAILABLE, not zero, and do not prevent backend boot.
Missing/expired tariff, credentials, unsupported model policy or budget causes
fallback rather than a paid call. An OpenAI provider slot reports unsupported;
it is not a fake implementation. The factory uses the existing configuration.

The initial reviewed model policy supports the existing default
`claude-sonnet-5`. Its documented context ceiling is used to conservatively
reserve up to 1,000,000 input tokens plus configured output tokens per attempt,
not an optimistic character/token estimate. Thinking is disabled; non-default
sampling parameters are unsupported for this model and are omitted. The actual
settings are recorded. This does **not** make LLM generation deterministic.
See the official [Sonnet 5 migration guide](https://platform.claude.com/docs/en/models/sonnet-5/migration-guide)
and [structured-output contract](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
Other model profiles must be verified before paid production use.

## Durable budget, failures and audit

Budget claims, a provider lease, a PENDING receipt and request audit commit before
HTTP. The exact structured request, including its schema/model/output settings,
is archived without secret headers. Daily accounting uses integer micro-USD and integer token counters, avoiding
floating-point arithmetic in admission checks. Limits cover the application UTC
request-initiation day. Costs are **owner-tariff estimates**, not verified provider
invoices or guarantees against an incorrectly configured tariff.

Known usage settles the reservation once, including a schema-invalid response if
usage is trustworthy. Timeout, transport failure, corrupt usage, or interruption
retains the full reservation and NULL cost. Unknown cost is never silently zero
and restart never resubmits a pending call. Old unresolved reservations remain in
their original day's ledger. There is no unaudited refund/reset mechanism.

The default token cap admits only one unresolved full-context reservation; a
second uncertain attempt may be budget-blocked even before the cost cap is reached.
Successful small calls release excess reservation. Each actual attempt has its
own receipt. At most one retry/re-ask is allowed; rate limits and timeouts cannot
cause an unbounded loop. Refusal is not re-asked. Persistent consecutive failures
open a durable cooldown circuit; a lease permits only one recovery probe at once.
Clock regression and conflicting leases fail closed for remote calls.

Responses are bounded in size and archived with redaction; a response hash and
parsed result are sealed in audit. Incomplete oversized/network responses remain
explicitly unavailable. Final receipt/budget/audit settlement is atomic: audit
storage failure rolls it back to PENDING and prevents returning approval.
Degradation requests a durable notification without claiming external delivery.

The existing authenticated workspace API and Decisions page show receipts,
unknown costs, current-day accounted/reserved amounts and token budgets. API
responses do not expose raw prompts, API keys or raw provider responses.

## Verification and remaining scope

The existing Market/F&O source panel also accepts owner-imported article JSON.
`POST /api/v1/news/sources/{slug}/articles` requires the current source audit version,
an enabled audited source, and an authenticated owner in PAPER mode. Required
article fields are `url`, `title`, `body`, timezone-aware `published_at` and explicit
`data_origin`. Submit actual source text only; fixtures belong in isolated tests.
The API never fetches a supplied URL, follows redirects or treats imported text as
instructions. Secret-shaped text and credential/query-bearing URLs are rejected.

Imports always remain `UNVERIFIED`, including Tier-1 sources. `OWNER_IMPORT` is
not proof of external provenance. Source attribution and policy version, original
text, publication time and server observation time are audit-linked. Repeating
an unchanged import returns its receipt; changed text creates another observation
without overwriting the original. This is version identity, not cross-source
semantic deduplication. `GET /api/v1/news/articles/{article_id}?as_of=...` validates
stored evidence against its audit seal and refuses reads before observation or
in the future. The current mutation path has no promotion to verified evidence.

On-demand acquisition is available through `POST /api/v1/news/sources/{slug}/poll`
with `expected_event_id`. It commits an audited attempt before any network call,
enforces `NEWS_POLL_INTERVAL_SECONDS` independently for each source across restart,
and atomically commits parsed article observations plus the completion receipt.
If any article is invalid, the batch rolls back and records a sanitized DEGRADED
receipt. Failed or cancelled requests do not bypass the durable cooldown. The
authenticated acquisition read API and existing Market/F&O controls expose pending,
degraded, stale, empty and acquired-but-unverified states. A crashed pending attempt
becomes degraded, not successful. No attempt counts as independent corroboration.

The fetcher resolves DNS once and pins a public IPv4 connection while retaining the
original Host and TLS SNI identity. It disables environment proxies and redirects,
requires HTTPS certificate validation, refuses compressed documents, and bounds
the complete request to 15 seconds and one megabyte. Mixed public/non-public DNS
answers are refused. IPv6-only endpoints currently stand down rather than using an
untested route. These behaviors use the documented
[HTTPX SNI extension](https://www.python-httpx.org/advanced/extensions/).

Supported source kinds are `rss` (UTF-8 RSS 2.0) and `api` (UTF-8 JSON with an
`articles` array, at most 100 items). JSON articles contain `url`, `title`, `body`
and timezone-aware `published_at`; the remote publisher cannot set `data_origin`,
verification or other execution state. RSS requires one title, link, description
and explicit timezone-aware pubDate per item; missing fields are not guessed.
XML uses [defusedxml](https://pypi.org/project/defusedxml/) with DTD, entities and
external references forbidden. HTML inside text remains data, never executable UI.
Unsupported Atom, XML encodings, authenticated endpoints, redirects, filing-specific
formats or provider-specific API schemas require explicit adapters, not silent fixes.

All acquisitions stay UNVERIFIED. `REMOTE_FEED` distinguishes actual fetch-path
observations from `OWNER_IMPORT`; isolated network fixtures explicitly use SYNTHETIC.
Automatic acquisition is opt-in with `NEWS_POLLING_ENABLED=true` and
`NEWS_ENABLED=true`, in PAPER only. FastAPI lifespan starts/stops the APScheduler
job; `NEWS_POLL_BATCH_SIZE` (default 5, maximum 20) bounds each cycle, and a rotating
source cursor prevents the first page starving later sources. The interval is a
minimum per-source cooldown and a batch tick, not a guarantee that every source
in a large universe is refreshed every tick. Batches larger than the configured
limit rotate over subsequent ticks. Slow jobs coalesce instead of overlapping.
Shutdown cancels active acquisition, leaving its committed attempt auditable;
restart respects the stored cooldown. Startup failure degrades this optional
service rather than stopping trading.

`GET /api/v1/news/runtime` and the existing Market/F&O view report actual scheduler
state. The non-critical `news_acquisition` check describes the latest bounded
batch, not a global source-health certificate. Acquisition alone is not
verification. Failed acquisition attempts atomically enqueue durable NEWS_DEGRADED notification
requests; that is not proof of delivery. No news scheduler path alters risk gates.
Derived quotation admission and declared strategy research are separate checks.
Source enablement is not an external connectivity claim.

Strategies declaring `news` now receive server-resolved research, not a caller's
availability flag or supplied news context. Reads normalize decision timestamps
to UTC before database filtering, reject future/stale/wrong-origin evidence and
reuse the existing configured-source corroboration checks. Where acquisition
history exists, only a recent successful receipt known at that decision timestamp
is eligible; future attempts cannot affect an earlier decision. Legacy evidence
without acquisition history retains its existing metadata-based verification
requirements, not an invented polling receipt or new external verification claim.

The shared pipeline independently repeats the dependency gate and requires
admitted NEWS citations. Reference decision audit records preserve the exact news
snapshots supplied to the strategy. Disabled or unavailable news produces a
negative decision instead of calling the news-dependent entry function; news-only
storage failures are degraded without imposing a new global trading latch.
Independent quantitative strategies do not query news and can still complete the
costed PAPER lifecycle while NEWS_ENABLED is false. A general database failure
remains critical: this does not bypass mandatory decision/audit persistence.

Authenticated `GET /api/v1/news/research/{instrument_id}` exposes the same research
admission view with explicit origin, cutoff and age bounds. AVAILABLE means that
stored evidence passes these deterministic checks, not that publishers or facts
were externally verified. Automatic acquired observations remain UNVERIFIED;
cross-source corroboration, entity mapping and grounded interpretation are still
required before automated news research can populate this view. Optional advisory
sentiment is controlled by a separate owner policy and cannot be a standalone trigger.

### Optional scheduled research

`NEWS_RESEARCH_ENABLED=true` opts the existing acquisition scheduler into research
production after each acquisition batch. `NEWS_ENABLED` and `NEWS_POLLING_ENABLED`
must also be enabled; PAPER remains mandatory. `NEWS_RESEARCH_BATCH_SIZE` defaults
to 2 and is bounded to 1–10 article candidates per tick. Production selects LIVE
observations only. SYNTHETIC provider/producer overrides exist for isolated tests,
not configuration that pretends fixture observations are live.

The producer reuses immutable observations, story/entity/conflict checks, the
existing Claude task and shared durable cost/quota/circuit controls. It requires
configured eligible sources, current acquisition readiness, non-stale publication
and a bounded mapped instrument set. A committed NEWS_PRODUCTION_STARTED record
claims each group version before any paid request. Concurrent/restarted workers
do not automatically repeat an ambiguous request. A matching saved interpretation
can finish admission without another request; a missing outcome reports
RECOVERY_REQUIRED. Admission receipts and the final job event commit atomically.

Unavailable outcomes are durable, not an automatic retry loop. The authenticated
Market recovery panel and `GET /api/v1/news/production/{group_event_id}` inspect the
persisted job. POST to the same path plus `/control` requires the current audit head,
a meaningful reason, and one of RECOVER, ABANDON or AUTHORIZE_RETRY. All controls
are PAPER-only, optimistic/version-checked and audited with the authenticated actor.

RECOVER only consumes a matching saved interpretation; it never calls Claude.
ABANDON prevents further automatic work/admission on that group version. It cannot
unsend an already-claimed request, refund a reservation, reconcile external billing
or revoke previously admitted evidence. Provider outcomes and budget reservations
remain untouched. A response arriving after abandonment cannot automatically admit
new quotations. Do not delete job audit records to retry.

AUTHORIZE_RETRY is restricted to known no-call failures: unavailable provider or
credentials, missing tariff/model, unsupported budget storage, open/busy circuit or
exhausted budget. The corresponding interpretation receipt must prove no call ID.
Pending attempts, timeouts and ambiguous paid failures cannot be retried through
this control. `NEWS_RESEARCH_MAX_OWNER_RETRIES` defaults to 1 (0–3 allowed) per
group version. Authorization expires after an owner-visible 1–900 second window
(300 by default) and is atomically consumed by one new attempt. The opted-in
scheduler and all original source/budget checks still apply. Expiration during
claim persistence rolls back the claim before any paid request.

External billing reconciliation remains unimplemented. New group versions are new
bounded work, not replay of the old job. Manual interpretation/admission controls
remain explicit separate owner actions and do not erase job history.

`GET /api/v1/news/runtime` and the Market view expose the latest bounded research
batch, statuses and admission IDs. `PER_INSTRUMENT_CHECK_REQUIRED` is not global
news availability: each consumer rechecks evidence at its decision timestamp.
Raw observations remain UNVERIFIED, and admitted quotations are source-policy
evidence, not proven truth or permission to trade. Model confidence remains an
uncalibrated self-report. Sentiment scoring requires a separately versioned,
explicitly accepted advisory policy; unknown direction remains unavailable.

`tests/unit/test_llm.py` exercises the real HTTP adapter against isolated
deterministic transport fixtures, strict parsing, timeout cancellation, prompt
boundaries and factory behavior. `tests/integration/test_llm_service.py` exercises
concurrent reservation, engine restart, circuit recovery, budget denial, atomic
settlement, migration/downgrade protection and the actual worker lifecycle:
market fixture → analysis/strategy → review → validation/sizing/risk → PAPER
entry/fill → protection/exit → costed FIFO/journal → API.

Only external market data and Anthropic HTTP are fixtures. They are not real
market or Claude verification. Real Anthropic connectivity/billing, deployed
PostgreSQL behavior, Groww LIVE and notification delivery remain unverified.
This is the first reference-review workload, not the completed general research
agent layer. The registered proposal task and receipt-bound decision path are
integrated; broader research/schema dispatch, source-specific agents and full
adversarial failure parity remain pending. Strict malformed JSON is rejected and
re-asked rather than extracted from arbitrary prose. LLM ledger items therefore
remain partial instead of claiming the entire AI layer complete.

Budget reservation denial emits a `LLM_BUDGET_EXHAUSTED` WARNING through the
existing transactional notification outbox, linked to the exact unavailable
advisory audit receipt and correlation ID. Missing tariff/model configuration
remains `LLM_DEGRADED`, not claimed spend exhaustion. The warning means the
configured reservation cannot be authorized; it does not establish actual vendor
billing. No paid HTTP call is made, and an eligible deterministic fallback still
passes the normal decision/risk gates. External notification delivery remains
unverified; local receipt creation is not delivery acknowledgement.
