# Stored market charts

The existing Market / F&O view reads authenticated `/api/v1/market/instruments`
and `/api/v1/market/candles/{instrument_id}` endpoints. Catalog membership is not
tradability or execution permission. Inactive and restricted instruments are labelled.

Select an instrument, data origin and intraday interval (1, 5, 10, 60 or 240
minutes). The chart uses persisted candles from the existing ingestion store;
it does not manufacture live prices, missing candles or a strategy signal.
Changing interval refetches data. Polling runs every ten seconds with bounded
requests, and failures remove previously displayed values.

## Time and provenance

- Origin is required and isolated: LIVE, HISTORICAL, REPLAY or SYNTHETIC.
- Only bars whose full interval has closed at the requested cutoff are returned.
- Future cutoffs are refused. Observations require a known ingestion timestamp
  no later than the cutoff. Unknown-ingestion legacy rows are excluded.
- The store contains current revisions, not a versioned archive of overwritten
  candles. This endpoint cannot reconstruct earlier overwritten revisions.
- Invalid OHLC windows are withheld. Empty and stale observations are explicit;
  RECORDED means stored data, not a verified live connection.
- Gaps are not filled. Discontinuity counts include overnight/session breaks.

The backend computes SMA20 over the returned observations; warm-up values are
unavailable. The table preserves API decimal values. The chart converts them to
JavaScript numbers for display only, never for trading/accounting. The chart axis
is UTC and crosshair timestamps are IST, explicitly labelled in the view.

## Scope and evidence

API tests cover closed bars, future cutoffs, origin separation, ingestion-time
availability, invalid stored values and known SMA values. Actual PostgreSQL
acceptance checks timestamp filtering in rollback-only temporary tables. An
actual Edge test signs in, selects stored test observations, renders the real
canvas, verifies backend SMA values and switches to an unavailable interval.
Deterministic fixtures remain test-only, not real-market evidence.

Instrument fundamentals use the existing point-in-time manual source API. Select
an imported source or enter its identifier to inspect values, source/period/knowledge dates,
metric exclusions and backend scoring evidence. Stale metrics are withheld from
usable values and scores; record availability is not metric completeness. A
source change refetches; an instrument change clears prior evidence. No vendor
feed or automatic source selection is invented. The typed API includes the
query cutoff and freshness policy. Actual browser acceptance loads an imported
fixture metric, then selects a missing source and verifies the old value disappears.

Source discovery uses authenticated `/api/v1/fundamentals/{instrument_id}/sources`.
It returns at most 50 source receipts per page, selecting each source's latest
revision known and received by the cutoff. Future cutoffs are refused. Receipt
availability is not freshness or independent verification of vendor evidence.
Selecting a source inspects its current point-in-time detail. Receipt knowledge
and ingestion times remain distinct; late imports cannot appear in earlier
catalogs. Manual source inspection remains available when discovery fails.

FE-005 remains partial pending broader detail acceptance.
This is not FE-004's live WebSocket watchlist. Daily/weekly chart
intervals and advanced overlays are not included yet.

Rendering uses [TradingView Lightweight Charts](https://tradingview.github.io/lightweight-charts/docs).
The UI includes the required TradingView notice/link; see the upstream
[NOTICE](https://raw.githubusercontent.com/tradingview/lightweight-charts/master/NOTICE).

## Audited quote watchlist

The selected catalog instrument can be added to a bounded ten-instrument
view-session watchlist. `/api/v1/market/quotes/{instrument_id}?origin=...` reads
existing audited PAPER ingestion observations using the same stored-quote
validation as execution recovery. It never fetches or fabricates a broker quote.
Origin and instrument identities are checked; damaged audit evidence yields
INVALID_EVIDENCE without numerical values. Missing observations are UNAVAILABLE.

The configured quote freshness policy suppresses stale numerical values while
retaining the observation timestamp. Unknown change percentage/volume remains
unavailable, not zero. The UI independently withholds values unless the server
reports RECORDED. That status describes evidence, not a live broker connection.

The dashboard uses `/api/v1/market/stream` WebSocket updates. Browser origin must
match the configured allowlist; query parameters are refused. Authentication and
subscription arrive in the first message, never a token-bearing URL. The server
uses existing owner-session validation before reading and again before sending,
so revoked/expired sessions stop receiving observations. Missing/invalid auth
never receives market state. Connection errors return generic close codes, not
database details or credential values.

Each connection accepts one initial subscription (up to ten instruments), with
a five-second handshake deadline and bounded read/auth/send operations. Extra
subscription frames close the connection. A process permits at most twenty
connections; this is not a distributed/global resource quota. Snapshots re-read
audited evidence about every two seconds, including freshness transitions. This
is not a sub-second event-bus or vendor-tick forwarding claim.

The initial UI uses one stream per watched row. It makes at most three connection
attempts per row mount, uses bounded backoff, and clears values on disconnect.
Silent streams trigger a watchdog; authenticated three-second HTTP polling is
the explicitly labelled fallback. Cleanup closes subscriptions on removal/origin
changes. Vite's API proxy enables WebSocket upgrades; production reverse proxies
must preserve upgrades and the configured Origin allowlist. TLS deployments use WSS.

FE-004 and BE-009 remain partial: durable user preferences, broader stream events,
sub-second event delivery and externally verified live feed acceptance remain
pending. Deterministic provider fixtures verify ingestion/API/browser integration,
not real-market data.
