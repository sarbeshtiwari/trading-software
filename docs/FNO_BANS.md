# Dated F&O ban evidence

`GET /api/v1/risk/fno-bans?origin=...` and authenticated
`PUT /api/v1/risk/fno-bans` expose owner-admitted NSE report evidence on the existing
Risk page. This does not enable F&O trading: the production PAPER OMS still rejects
non-CASH entries, and CASH-specific cost/contract production remains unchanged.

## Source and ingestion

NSE describes a separate dated ban report, not the general regulatory-indicator
file. See the [NSE source page](https://www.nseindia.com/static/products-services/equity-derivatives-risk-management-sec-ban)
and [NSE circular, Annexure 2](https://archives.nseindia.com/content/circulars/FAOP37975.pdf).
The public source is [fo_secban.csv](https://nsearchives.nseindia.com/content/fo/fo_secban.csv).
A public HTTP read during implementation confirmed a dated header followed by
numbered symbol rows. It was not admitted into the application or used as a test
fixture. This read does not verify a scheduled production feed or broker execution.

The strict parser reads the trade date from the document, not the download date.
It rejects missing dates, invalid dates, HTML/error responses, malformed CSV,
duplicate symbols and nonsequential row numbers. Unknown formats fail closed.
The full original document and SHA-256 digest are retained in the audited admission.
A dated header without rows represents an explicitly admitted empty report; an
empty HTTP body never does. Operators must review the complete source document.

Admission requires explicit data origin, timezone-aware `known_at`, reason,
`ADMIT PAPER NSE BAN REPORT` confirmation and the reviewed `expected_event_id`.
The API does not fetch arbitrary URLs. Source provenance remains labelled
`OWNER_ADMITTED_SOURCE_NOT_AUTOMATICALLY_VERIFIED`.

## Selection and enforcement

Reports are versioned by audited receipt order with monotonically reviewed heads.
Conflicting revisions with the same knowledge timestamp and older replacements
for the same trade date are rejected. Selection uses the current IST trade date.
A tomorrow report does not clear today's list. A prior-day list never carries
forward as proof of a clear current session. Historical reads cannot see later
admissions; current clock rollback or corrupt audit history fails closed.

F&O entries require current evidence and an exact canonical underlying mapping.
All strikes and expiries sharing that underlying are covered; no symbol guessing
is performed. Missing reports, missing mappings and unsupported exchanges block
entries with named reasons. Caller `ban_listed=False` cannot bypass the server
veto. Shared decisions, preflight and final entry checks use the existing
instrument guard. Exits do not use these entry guards, and CASH instruments are
outside this report's scope.

## Remaining limitations

- Scheduled acquisition, BSE-specific source support and PostgreSQL cross-process
  race verification are not implemented or externally verified here.
- F&O positive OMS/fill/protection/fee/FIFO lifecycle is still disabled; tests do
  not misrepresent a CASH exit as verified F&O execution.
- Broader regulatory restrictions remain distinct from this one NSE report.
- EXCH-008 and RISK-014 remain partial. PAPER is default; Groww LIVE is unverified.
