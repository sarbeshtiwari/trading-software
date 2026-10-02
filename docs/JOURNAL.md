# Recorded journal workflow

The existing Journal page reads authenticated application records, not dashboard
fixtures. It refreshes every ten seconds and distinguishes loading, empty,
unavailable/stale and integrity-failure states. No additional page is introduced.

## Application integration

Shared quantitative/LLM pipeline negative outcomes are recorded transactionally as
`REJECTION` entries, including pre-proposal validation, sizing and risk decisions.
They retain candidate identity, recorded context and the actual rejection stage or
binding risk rule. They have no invented order, fill, quantity or P&L. A journal
audit-write failure rolls back the associated decision transaction.

Closed PAPER fills retain the existing transactionally sealed journal. The journal
also records actual holding duration, adverse entry slippage, weighted recorded
exit slippage, approved risk and net R-multiple where available. A quantitative
strategy is not labelled as an AI thesis. Unknown charges mean unknown net P&L and
outcome, not zero costs. Costs remain configured estimates, not broker invoices.

## Authenticated API

- `GET /api/v1/journal`: typed records, annotations and integrity classification.
  Filters: mode, kind, strategy_id, instrument_id, outcome, start/end dates.
  Offset pagination defaults to 50, maximum 200. Date bounds are inclusive local
  Asia/Kolkata days, converted to UTC before querying.
- `GET /api/v1/journal/{id}`: recorded proposal, candidate where linked, sizing,
  risk/preflight decisions, orders, fills, transitions, position, regime and audit.
  Missing sources are listed as gaps; legacy/unbound journals are not certified.
  `AVAILABLE` describes recorded lineage availability, not external verification
  or proof that an administrator has never altered the database.
- `POST /api/v1/journal/{id}/annotations`: owner note, optional tags and mandatory
  reason. Notes and their audit snapshots commit together. Economic fields are
  unchanged. Added notes are attributed to the authenticated owner, timestamped
  and secret-redacted; deleted or altered audited notes fail integrity checks.
- `POST /api/v1/journal/{id}/corrections`: append a contextual version with the
  expected current version and a reason. Only owner plan-adherence assessment on
  a trade or explanatory rejection detail is accepted. Economic fields, execution
  identifiers, actual rejection codes and source evidence are not editable.
- `GET /api/v1/journal/export/json` and `/export/csv`: all filtered records,
  including every persisted journal column, annotations and integrity status.
  Pagination is rejected for exports. Maximum 2,000 records and 16 MiB; exceeding
  either limit refuses the export rather than silently truncating it. Each export
  records its filters, row count and payload digest in the audit trail.

The frontend downloads with authenticated requests and bounded session refresh;
credentials never appear in a download URL. Export responses are `no-store`.

## Immutable versions

Application ORM attribute updates, deletes and bulk journal mutations are refused.
Migration `0010_journal_revisions` also installs append-only UPDATE/DELETE triggers
on journal entries, annotations and revision links. The migration is exercised
against SQLite; PostgreSQL trigger execution remains externally unverified.
Upgrade an existing database before starting the new application:

```powershell
cd backend
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Corrections create a new sealed journal row plus a unique root/version and
previous-entry link, committed with the original chain's correction audit. The
original row and its digest never change, including the legacy `superseded_by`
column. Use `revision_root_id`, `previous_version_id`, `latest_version_id` and the
detail response's audited `revisions` instead. Original annotations stay attached
to their original version and remain accessible in the same Journal view.

Stale or concurrent correction attempts cannot fork the version chain. A failed
audit rolls back the new version and link together. Deleted or modified revision
links cause list/detail/export integrity refusal. Corrected records are labelled
`OWNER_CONTEXT_CORRECTION`, not new trades. Workspace and historical economic
totals use original execution journals only. PAPER evidence eligibility excludes
both a corrected original and its revisions pending review; a metadata correction
cannot create fresh performance evidence or grant trading permission.

Exports include version metadata. Do not sum versions of the same journal as
separate trades. Owner contextual assessments are explicitly subjective; this
interface cannot rewrite cash, fills, fees, P&L or a model's actual original output.

### Lossless CSV encoding

Each CSV cell contains a JSON value. Parse the CSV normally, then JSON-decode each
cell. This preserves nulls, nested context, decimal strings, annotation lists and
timestamps exactly. It also prevents a user note beginning with `=`, `+`, `-` or
`@` from being interpreted as a spreadsheet formula: text is a JSON string literal.
For example, in Python:

```python
records = [
    {key: json.loads(value) for key, value in row.items()}
    for row in csv.DictReader(export_file)
]
```

## Verification and limitations

`tests/integration/test_journal_api.py` drives the actual reference worker, shared
pipeline, PAPER fills, authenticated APIs, annotations, exports, local-day filters,
audit failure rollback and tamper checks. The real browser target-exit scenario
opens the resulting journal lineage, adds an owner annotation and parses the
downloaded JSON artifact. These are isolated deterministic tests, not externally
observed trades or delivered notification evidence.

No economic update endpoint exists. Legacy records remain explicitly unbound and
cannot be corrected into apparently verified evidence. Revision integration tests
cover ORM/bulk refusal, concurrent requests, audit rollback, revision deletion,
evidence exclusion and real SQLite migration triggers/downgrade. Out-of-band
corruption tests deliberately use the un-migrated model-fixture database to bypass
application safeguards and test detection; that is not a production mutation path.
The browser creates an owner-assessment version and returns to the unchanged
original. Missing news, options context or AI evidence is never manufactured.
General cross-mode journal completion, PostgreSQL verification, M1/M2 and Groww
LIVE remain unverified.
