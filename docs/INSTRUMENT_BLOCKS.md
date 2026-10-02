# PAPER instrument entry controls

An authenticated owner can inhibit new entries for an exact canonical instrument
ID across every PAPER data origin. This is not a global kill switch, an exit
request or a verified exchange ban list. No symbol, capital, reason or control
confirmation is assumed. Other trading modes have no mutation path here.

## API and review

- `GET /api/v1/risk/instrument-blocks` lists recorded manual controls and current
  restricted catalog instruments, including released manual control histories.
- `GET /api/v1/risk/instrument-blocks/{instrument_id}` loads the review snapshot.
  Missing instruments or invalid/future-dated control evidence are unavailable,
  not an unblocked success.
- `PUT /api/v1/risk/instrument-blocks/{instrument_id}` requires a strict boolean
  `blocked`, the reviewed `expected_event_id`, a meaningful `reason`, and exactly
  `BLOCK PAPER INSTRUMENT` or `UNBLOCK PAPER INSTRUMENT` as `confirmation`.

The existing Risk page shows current manual/catalog state and loads a separate
reviewed version before mutation. Polling the list does not silently advance the
version used to authorize a release. Changing the selected instrument clears the
review; failed writes require loading it again. Concurrent changes are rejected
by the backend, not overwritten optimistically.

## Persistence and enforcement

Manual transitions use the transactional hash-verified audit ledger. They retain
instrument, actor, reason, expected predecessor and catalog snapshot. Blocking
also creates a durable notification request in the same transaction. Delivery is
not claimed. Explicit release appends a record; it does not erase history or clear
news halts, account latches, catalog restrictions or other instrument controls.
Restrictions survive restart and have no automatic expiry.

The shared proposal pipeline and entry freshness checks read server-held controls;
transactional approval safety and final dispatch check again. Rejections are named
`MANUALLY_BLOCKED`, `INSTRUMENT_RESTRICTED` or `INSTRUMENT_INACTIVE`. A caller's
false market flags cannot override these checks. Expected instrument vetoes do not
become global risk-engine errors. Invalid audit/catalog evidence fails closed.

Current catalog restrictions and active flags are rechecked after approval, so a
proposal cannot retain permission after the catalog changes. A manual release
does not reactivate an inactive instrument or remove a catalog restriction.
Protective exits do not consult these entry-only gates. An unsent CREATED intent
blocked at final dispatch is not resubmitted; existing recovery/hygiene handles
its disposition and duplicate calls do not submit a second order.

## Limits

Catalog evidence is explicitly labelled `CURRENT_CATALOG_NOT_VERIFIED_EXCHANGE_BAN`.
It is current operational metadata, not a historical exchange-ban time series.
These controls do not supply historical backtest eligibility or map an underlying
to all of its derivative contracts. Full EXCH-008 ban-period ingestion and remaining
server-resolved event-calendar enforcement are pending. RISK-014 stays partial.

Tests cover real shared-pipeline and PAPER execution vetoes, a block arriving after
preflight, duplicate execution, restart, protected exits, catalog changes, owner
authentication and actual React/browser controls. Deployed PostgreSQL concurrency,
remote notifications and Groww LIVE remain unverified. Existing audit-chain
external-head/privileged-deletion limitations still apply. No M1/M2 claim is made.
