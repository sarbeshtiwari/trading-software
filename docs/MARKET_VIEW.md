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
